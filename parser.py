"""
Streaming parser for Apple's Health export.xml.

The file is one <HealthData> root holding a flat list of <Record>, <Workout>
and <ActivitySummary> children. Real exports run to gigabytes, so we never build
the whole tree. iterparse hands us each element as it closes; we read its
attributes, queue an insert, then clear the root so parsed elements don't pile
up. Memory stays flat whether the file is 20 MB or 5 GB.

Import is a merge: INSERT OR IGNORE against the uniqueness indexes in schema.sql.
Loading the same export twice changes nothing; a fresher export only adds rows
that weren't there.
"""
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone

BATCH = 5000  # rows per executemany; bigger isn't faster once the disk is the limit

RECORD_COLS = ("type", "source_name", "source_version", "device", "unit",
               "value", "value_num", "start_date", "end_date", "creation_date")


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse(xml_path, db_path, source="upload"):
    """Merge one export.xml into the database at db_path. Runs in its own thread,
    so it opens its own connection (sqlite connections can't cross threads)."""
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        _run(conn, xml_path, source)
    except Exception as exc:
        conn.execute(
            "UPDATE import_status SET state = 'error', message = ?, finished_at = ? WHERE id = 1",
            (str(exc)[:1000], _now()),
        )
        conn.commit()
        raise
    finally:
        conn.close()


def _run(conn, xml_path, source):
    conn.execute(
        "UPDATE import_status SET state = 'running', records_seen = 0, records_new = 0, "
        "current_type = NULL, source = ?, message = NULL, started_at = ?, finished_at = NULL "
        "WHERE id = 1",
        (source, _now()),
    )
    conn.commit()

    insert_record = (
        f"INSERT OR IGNORE INTO records ({','.join(RECORD_COLS)}) "
        f"VALUES ({','.join('?' * len(RECORD_COLS))})"
    )

    batch = []
    seen = 0
    new_before = conn.total_changes

    # events=("start",) once to grab the root, then "end" for everything else.
    context = ET.iterparse(xml_path, events=("start", "end"))
    _, root = next(context)

    for event, elem in context:
        if event != "end":
            continue
        tag = elem.tag

        if tag == "Record":
            a = elem.attrib
            batch.append((
                a.get("type"), a.get("sourceName"), a.get("sourceVersion"),
                a.get("device"), a.get("unit"), a.get("value"), _num(a.get("value")),
                a.get("startDate"), a.get("endDate"), a.get("creationDate"),
            ))
            seen += 1
            if len(batch) >= BATCH:
                conn.executemany(insert_record, batch)
                batch.clear()
                conn.execute(
                    "UPDATE import_status SET records_seen = ?, records_new = ?, current_type = ? WHERE id = 1",
                    (seen, conn.total_changes - new_before, a.get("type")),
                )
                conn.commit()

        elif tag == "Workout":
            a = elem.attrib
            # Older exports carried energy/distance as Workout attributes; newer
            # ones moved them into <WorkoutStatistics> children. Start from the
            # attributes (usually empty now) and let the children override.
            energy = _num(a.get("totalEnergyBurned"))
            energy_unit = a.get("totalEnergyBurnedUnit")
            distance = _num(a.get("totalDistance"))
            distance_unit = a.get("totalDistanceUnit")
            for ch in elem:
                if _local(ch.tag) != "WorkoutStatistics":
                    continue
                stat_type = ch.get("type")
                total = _num(ch.get("sum"))
                if total is None:
                    continue
                if stat_type == "HKQuantityTypeIdentifierActiveEnergyBurned":
                    energy, energy_unit = total, ch.get("unit")
                elif stat_type in (
                    "HKQuantityTypeIdentifierDistanceWalkingRunning",
                    "HKQuantityTypeIdentifierDistanceCycling",
                    "HKQuantityTypeIdentifierDistanceSwimming",
                ):
                    distance, distance_unit = total, ch.get("unit")
            # OR REPLACE (not IGNORE) so re-importing refreshes a workout that
            # was stored before this fix and had no energy/distance.
            conn.execute(
                "INSERT OR REPLACE INTO workouts (activity_type, duration, duration_unit, "
                "total_distance, distance_unit, total_energy, energy_unit, start_date, "
                "end_date, source_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (a.get("workoutActivityType"), _num(a.get("duration")), a.get("durationUnit"),
                 distance, distance_unit, energy, energy_unit,
                 a.get("startDate"), a.get("endDate"), a.get("sourceName")),
            )

        elif tag == "ActivitySummary":
            # Apple has renamed some of these attributes across iOS versions;
            # .get() just returns None for the ones a given export doesn't carry.
            a = elem.attrib
            conn.execute(
                "INSERT OR IGNORE INTO activity_summary (date, active_energy, active_energy_goal, "
                "move_time, move_time_goal, exercise_time, exercise_time_goal, stand_hours, "
                "stand_hours_goal) VALUES (?,?,?,?,?,?,?,?,?)",
                (a.get("dateComponents"), _num(a.get("activeEnergyBurned")),
                 _num(a.get("activeEnergyBurnedGoal")), _num(a.get("appleMoveTime")),
                 _num(a.get("appleMoveTimeGoal")), _num(a.get("appleExerciseTime")),
                 _num(a.get("appleExerciseTimeGoal")), _num(a.get("appleStandHours")),
                 _num(a.get("appleStandHoursGoal"))),
            )

        else:
            # A child element closed (MetadataEntry, WorkoutEvent, and friends).
            # Skip it, and leave the root alone so we don't drop the parent we're
            # still inside.
            continue

        # A top-level element is stored. Nothing is half-read at this point, so
        # wiping the root frees everything parsed so far.
        root.clear()

    if batch:
        conn.executemany(insert_record, batch)

    conn.execute(
        "UPDATE import_status SET state = 'done', records_seen = ?, records_new = ?, "
        "current_type = NULL, finished_at = ? WHERE id = 1",
        (seen, conn.total_changes - new_before, _now()),
    )
    conn.commit()


# ---------- extras: GPS routes + ECG (only present in the zip) ----------

def _local(tag):
    # Strip the XML namespace: "{http://...}trkpt" -> "trkpt".
    return tag.rsplit("}", 1)[-1]


def parse_extras(zip_path, db_path):
    """Pull the workout-routes/*.gpx and electrocardiograms/*.csv files out of
    the export zip. Both dedup on filename, so re-importing skips what's there."""
    if not zipfile.is_zipfile(zip_path):
        return
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        with zipfile.ZipFile(zip_path) as z:
            for name in z.namelist():
                low = name.lower()
                if low.endswith(".gpx"):
                    _gpx(conn, name.rsplit("/", 1)[-1], z.read(name))
                elif low.endswith(".csv") and "electrocardiogram" in low:
                    _ecg(conn, name.rsplit("/", 1)[-1], z.read(name).decode("utf-8", "replace"))
        conn.commit()
    finally:
        conn.close()


def _gpx(conn, filename, data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return
    points = []
    for el in root.iter():
        if _local(el.tag) != "trkpt":
            continue
        lat, lon = el.get("lat"), el.get("lon")
        ele = t = None
        for ch in el:
            if _local(ch.tag) == "ele":
                ele = ch.text
            elif _local(ch.tag) == "time":
                t = ch.text
        points.append((_num(lat), _num(lon), _num(ele), t))
    if not points:
        return
    cur = conn.execute(
        "INSERT OR IGNORE INTO routes (filename, start_date, point_count) VALUES (?, ?, ?)",
        (filename, points[0][3], len(points)),
    )
    if cur.rowcount == 0:
        return  # already imported
    rid = cur.lastrowid
    conn.executemany(
        "INSERT INTO route_points (route_id, lat, lon, ele, t) VALUES (?, ?, ?, ?, ?)",
        [(rid, p[0], p[1], p[2], p[3]) for p in points],
    )


def _ecg(conn, filename, text):
    # An Apple ECG csv is a few "key,value" header lines then one voltage sample
    # per line. Locale varies (headers and even the decimal comma), so we treat
    # any line that parses as a number as a sample and everything else as header.
    samples = []
    meta = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            samples.append(float(line.replace(",", ".")))
            continue
        except ValueError:
            pass
        if "," in line:
            k, v = line.split(",", 1)
            meta[k.strip().lower()] = v.strip()
    if not samples:
        return

    recorded = classification = unit = None
    rate = None
    for k, v in meta.items():
        if recorded is None and ("recorded" in k or "date" in k or "dato" in k):
            recorded = v
        if classification is None and ("classif" in k or "klassif" in k):
            classification = v
        if rate is None and ("sample" in k or "rate" in k or "frekvens" in k):
            m = re.search(r"[\d.]+", v)
            rate = float(m.group(0)) if m else None
        if unit is None and ("unit" in k or "enhet" in k):
            unit = v

    cur = conn.execute(
        "INSERT OR IGNORE INTO ecg (filename, recorded_date, classification, sample_rate, "
        "duration_s, unit) VALUES (?, ?, ?, ?, ?, ?)",
        (filename, recorded, classification, rate,
         (len(samples) / rate) if rate else None, unit),
    )
    if cur.rowcount == 0:
        return
    eid = cur.lastrowid
    conn.executemany(
        "INSERT INTO ecg_samples (ecg_id, idx, uv) VALUES (?, ?, ?)",
        [(eid, i, s) for i, s in enumerate(samples)],
    )
