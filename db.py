"""
Storage layer for AHDX.

Two kinds of database live under the data volume:

  registry.db          the list of health databases and which one is active
  databases/<name>.db  one per health dataset (per person, per year, whatever)

Keeping them as separate files means you can hand someone a single .db and it's
their whole dataset, nothing else tangled in. Everything here is plain stdlib
sqlite3, no ORM.
"""
import os
import re
import sqlite3
from contextlib import contextmanager

DATA_DIR = os.environ.get("AHDX_DATA", "/data")
DB_DIR = os.path.join(DATA_DIR, "databases")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
INBOX_DIR = os.path.join(DATA_DIR, "inbox")
INBOX_DONE = os.path.join(INBOX_DIR, "done")
REGISTRY = os.path.join(DATA_DIR, "registry.db")
SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")


def _ensure_dirs():
    for d in (DATA_DIR, DB_DIR, UPLOAD_DIR, INBOX_DIR, INBOX_DONE):
        os.makedirs(d, exist_ok=True)


@contextmanager
def _open(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    _ensure_dirs()
    with _open(REGISTRY) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS databases (
                 id         INTEGER PRIMARY KEY AUTOINCREMENT,
                 name       TEXT NOT NULL UNIQUE,
                 filename   TEXT NOT NULL UNIQUE,
                 created_at TEXT NOT NULL DEFAULT (datetime('now')),
                 is_active  INTEGER NOT NULL DEFAULT 0
               )"""
        )
        # Small app-wide key/value store: the GUI password hash, the session
        # secret, anything that isn't health data.
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    # A fresh install has nothing, so give it one database to land data in.
    if not list_databases():
        create_database("My Health")
    if get_active() is None:
        set_active(list_databases()[0]["id"])

    # Re-apply the schema to every database on startup. It's all CREATE ... IF
    # NOT EXISTS, so this is a no-op on current databases and quietly adds new
    # tables (routes, ecg, ...) to ones made by an older version.
    for row in list_databases():
        apply_schema(os.path.join(DB_DIR, row["filename"]))


# ---------- settings + GUI password ----------

def get_setting(key, default=None):
    with _open(REGISTRY) as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with _open(REGISTRY) as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def has_password():
    return get_setting("password_hash") is not None


def set_password(pw):
    from werkzeug.security import generate_password_hash
    # pbkdf2 rather than the newer scrypt default, so it hashes on any Python
    # build (scrypt needs an OpenSSL that isn't always compiled in).
    set_setting("password_hash", generate_password_hash(pw, method="pbkdf2:sha256"))


def check_password(pw):
    from werkzeug.security import check_password_hash
    h = get_setting("password_hash")
    return bool(h and check_password_hash(h, pw))


# ---------- the database registry ----------

def list_databases():
    with _open(REGISTRY) as c:
        return c.execute("SELECT * FROM databases ORDER BY created_at, id").fetchall()


def get_active():
    with _open(REGISTRY) as c:
        return c.execute("SELECT * FROM databases WHERE is_active = 1").fetchone()


def set_active(db_id):
    with _open(REGISTRY) as c:
        c.execute("UPDATE databases SET is_active = 0")
        c.execute("UPDATE databases SET is_active = 1 WHERE id = ?", (db_id,))


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "db"


def create_database(name):
    name = name.strip()
    if not name:
        raise ValueError("A database needs a name.")
    with _open(REGISTRY) as c:
        if c.execute("SELECT 1 FROM databases WHERE name = ?", (name,)).fetchone():
            raise ValueError("A database with that name already exists.")

    # Pick a filename that doesn't collide with one already on disk.
    base = _slug(name)
    filename = base + ".db"
    n = 1
    while os.path.exists(os.path.join(DB_DIR, filename)):
        filename = f"{base}-{n}.db"
        n += 1

    apply_schema(os.path.join(DB_DIR, filename))
    with _open(REGISTRY) as c:
        cur = c.execute(
            "INSERT INTO databases (name, filename) VALUES (?, ?)", (name, filename)
        )
        new_id = cur.lastrowid
    set_active(new_id)
    return new_id


def _human_size(n):
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def database_overview():
    """The registry rows plus, for each database, its file size and a few
    counts. Opens each .db file, so it's a page-load query, not a hot path."""
    out = []
    for row in list_databases():
        path = os.path.join(DB_DIR, row["filename"])
        info = {
            "id": row["id"], "name": row["name"], "filename": row["filename"],
            "created_at": row["created_at"], "is_active": row["is_active"],
            "size": os.path.getsize(path) if os.path.exists(path) else 0,
            "records": 0, "workouts": 0, "last_import": None,
        }
        info["size_h"] = _human_size(info["size"])
        try:
            with _open(path) as c:
                info["records"] = c.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"]
                info["workouts"] = c.execute("SELECT COUNT(*) AS n FROM workouts").fetchone()["n"]
                st = c.execute("SELECT finished_at FROM import_status WHERE id = 1").fetchone()
                info["last_import"] = st["finished_at"] if st else None
        except sqlite3.Error:
            pass
        out.append(info)
    return out


def rename_database(db_id, new_name):
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("A database needs a name.")
    with _open(REGISTRY) as c:
        clash = c.execute(
            "SELECT 1 FROM databases WHERE name = ? AND id <> ?", (new_name, db_id)
        ).fetchone()
        if clash:
            raise ValueError("A database with that name already exists.")
        c.execute("UPDATE databases SET name = ? WHERE id = ?", (new_name, db_id))


def delete_database(db_id):
    with _open(REGISTRY) as c:
        row = c.execute("SELECT * FROM databases WHERE id = ?", (db_id,)).fetchone()
    if not row:
        return
    if len(list_databases()) <= 1:
        raise ValueError("This is the only database, so there's nothing to switch to. Create another first.")

    path = os.path.join(DB_DIR, row["filename"])
    if os.path.exists(path):
        os.remove(path)
    with _open(REGISTRY) as c:
        c.execute("DELETE FROM databases WHERE id = ?", (db_id,))
    if row["is_active"]:
        set_active(list_databases()[0]["id"])


def apply_schema(path):
    conn = sqlite3.connect(path)
    try:
        with open(SCHEMA, encoding="utf-8") as f:
            conn.executescript(f.read())
    finally:
        conn.close()


# ---------- the active health database ----------

def active_path():
    a = get_active()
    return os.path.join(DB_DIR, a["filename"])


@contextmanager
def health():
    with _open(active_path()) as c:
        yield c


def resolve_db(name):
    """Path for a database by name or filename. None (or an unresolved Grafana
    variable like "${db}") means the active one; an unknown name raises KeyError
    so the API can answer 404."""
    if not name or name.startswith("$"):
        return active_path()
    with _open(REGISTRY) as c:
        row = c.execute(
            "SELECT filename FROM databases WHERE lower(name) = lower(?) OR lower(filename) = lower(?)",
            (name, name),
        ).fetchone()
    if not row:
        raise KeyError(name)
    return os.path.join(DB_DIR, row["filename"])


@contextmanager
def open_db(name=None):
    with _open(resolve_db(name)) as c:
        yield c


# ---------- read API queries (pick the database by name) ----------

def api_types(name=None):
    with open_db(name) as c:
        return [dict(r) for r in c.execute(
            "SELECT type, COUNT(*) AS n, MIN(start_date) AS first, MAX(start_date) AS last "
            "FROM records GROUP BY type ORDER BY n DESC"
        )]


def api_records(name, type_, start, end, limit):
    q = ("SELECT type, value, value_num, unit, source_name, start_date, end_date "
         "FROM records WHERE type = ?")
    params = [type_]
    if start:
        q += " AND start_date >= ?"
        params.append(start)
    if end:
        q += " AND start_date <= ?"
        params.append(end)
    q += " ORDER BY start_date LIMIT ?"
    params.append(limit)
    with open_db(name) as c:
        return [dict(r) for r in c.execute(q, params)]


def api_daily(name, type_, agg, start, end):
    """One value per day, shaped as [{"time": "...", "value": n}] so a Grafana
    JSON/Infinity datasource can read it straight."""
    expr = _AGG.get(agg, _AGG["avg"])
    where = "type = ?"
    params = [type_]
    if agg != "count":
        where += " AND value_num IS NOT NULL"
    if start:
        where += " AND start_date >= ?"
        params.append(start)
    if end:
        where += " AND start_date <= ?"
        params.append(end)
    q = (f"SELECT substr(start_date, 1, 10) AS time, {expr} AS value "
         f"FROM records WHERE {where} GROUP BY time ORDER BY time")
    with open_db(name) as c:
        return [dict(r) for r in c.execute(q, params) if r["time"]]


def api_workouts(name=None):
    with open_db(name) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT activity_type, duration, duration_unit, total_distance, distance_unit, "
            "total_energy, energy_unit, start_date, end_date, source_name "
            "FROM workouts ORDER BY start_date DESC"
        )]
    for r in rows:
        if r["activity_type"]:  # "HKWorkoutActivityTypeWalking" -> "Walking"
            r["activity_type"] = r["activity_type"].replace("HKWorkoutActivityType", "")
    return rows


def api_activity(name=None):
    with open_db(name) as c:
        return [dict(r) for r in c.execute(
            "SELECT date AS time, active_energy, active_energy_goal, move_time, "
            "exercise_time, stand_hours FROM activity_summary ORDER BY date"
        )]


def _parse_dt(s):
    from datetime import datetime
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")  # "... +0200"
    except (ValueError, TypeError):
        return None


def api_sleep(name=None, start=None, end=None):
    """One row per night with hours per stage and a 0–100 score. Apple has no
    native sleep score, so we make one from duration, efficiency, and (when the
    watch recorded stages) how much deep/REM sleep there was. Older data only
    has "asleep vs in bed", so the score falls back to duration + efficiency."""
    from collections import defaultdict
    from datetime import timedelta
    q = ("SELECT value, start_date, end_date FROM records "
         "WHERE type = 'HKCategoryTypeIdentifierSleepAnalysis'")
    params = []
    if start:
        q += " AND start_date >= ?"; params.append(start)
    if end:
        q += " AND start_date <= ?"; params.append(end)
    with open_db(name) as c:
        rows = c.execute(q, params).fetchall()

    nights = defaultdict(lambda: dict(in_bed=0.0, awake=0.0, deep=0.0, rem=0.0, core=0.0, unspec=0.0))
    for r in rows:
        s, e = _parse_dt(r["start_date"]), _parse_dt(r["end_date"])
        if not s or not e:
            continue
        hours = (e - s).total_seconds() / 3600.0
        if hours <= 0 or hours > 16:
            continue
        # Assign to a night by shifting 18h back, so an evening + the morning
        # after it land on the same date.
        night = (s - timedelta(hours=18)).date().isoformat()
        v = r["value"] or ""
        if v.endswith("InBed"):
            nights[night]["in_bed"] += hours
        elif v.endswith("Awake"):
            nights[night]["awake"] += hours
        elif v.endswith("AsleepDeep"):
            nights[night]["deep"] += hours
        elif v.endswith("AsleepREM"):
            nights[night]["rem"] += hours
        elif v.endswith("AsleepCore"):
            nights[night]["core"] += hours
        elif "Asleep" in v:
            nights[night]["unspec"] += hours

    out = []
    for night in sorted(nights):
        d = nights[night]
        asleep = d["deep"] + d["rem"] + d["core"] + d["unspec"]
        in_bed = d["in_bed"] if d["in_bed"] > 0 else asleep + d["awake"]
        has_stages = (d["deep"] + d["rem"] + d["core"]) > 0
        dur = min(asleep / 8.0, 1.0)                 # 8h asleep = full marks
        eff = min(asleep / in_bed, 1.0) if in_bed else dur
        if has_stages:
            deep_s = min((d["deep"] / asleep) / 0.16, 1.0) if asleep else 0
            rem_s = min((d["rem"] / asleep) / 0.22, 1.0) if asleep else 0
            score = 100 * (0.40 * dur + 0.25 * eff + 0.175 * deep_s + 0.175 * rem_s)
        else:
            score = 100 * (0.60 * dur + 0.40 * eff)
        out.append({
            "time": night, "asleep_h": round(asleep, 2), "in_bed_h": round(in_bed, 2),
            "deep_h": round(d["deep"], 2), "rem_h": round(d["rem"], 2),
            "core_h": round(d["core"], 2), "unspecified_h": round(d["unspec"], 2),
            "awake_h": round(d["awake"], 2), "efficiency": round(100 * eff),
            "score": round(score),
        })
    return out


def api_routes(name=None):
    with open_db(name) as c:
        return [dict(r) for r in c.execute(
            "SELECT id, filename, start_date, point_count FROM routes ORDER BY start_date DESC"
        )]


def _haversine_km(a_lat, a_lon, b_lat, b_lon):
    import math
    r = 6371.0
    dlat, dlon = math.radians(b_lat - a_lat), math.radians(b_lon - a_lon)
    h = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(h))


def api_route_stats(name, route_id=None):
    """Distance, duration, elevation gain and average speed for one route,
    worked out from its points. No id -> the latest route (matches the map)."""
    from datetime import datetime
    with open_db(name) as c:
        if not route_id:
            latest = c.execute("SELECT id FROM routes ORDER BY start_date DESC LIMIT 1").fetchone()
            if not latest:
                return {}
            route_id = latest["id"]
        route = c.execute("SELECT filename, start_date FROM routes WHERE id = ?", (route_id,)).fetchone()
        pts = c.execute(
            "SELECT lat, lon, ele, t FROM route_points WHERE route_id = ? ORDER BY rowid", (route_id,)
        ).fetchall()
        workouts = c.execute(
            "SELECT activity_type, total_energy, start_date FROM workouts").fetchall()
    if not route or not pts:
        return {}

    dist = 0.0
    gain = 0.0
    prev = prev_ele = None
    for p in pts:
        if p["lat"] is None or p["lon"] is None:
            continue
        if prev is not None:
            dist += _haversine_km(prev[0], prev[1], p["lat"], p["lon"])
        prev = (p["lat"], p["lon"])
        if p["ele"] is not None:
            if prev_ele is not None and p["ele"] > prev_ele:
                gain += p["ele"] - prev_ele
            prev_ele = p["ele"]

    def _t(s):
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
            try:
                return datetime.strptime(s, fmt)
            except (ValueError, TypeError):
                pass
        return None

    times = [t for t in (_t(p["t"]) for p in pts) if t]
    dur_min = (times[-1] - times[0]).total_seconds() / 60.0 if len(times) >= 2 else None
    speed = dist / (dur_min / 60.0) if dur_min else None

    # The GPX has no calories, so match this route to its workout by start time
    # (within an hour) and borrow the workout's energy. Route timestamps are
    # UTC ("...Z"); workout start dates carry a timezone offset.
    from datetime import datetime, timezone

    def _utc(s):
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z").astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None

    route_start = _utc(pts[0]["t"]) or _utc(route["start_date"])
    energy = activity = None
    if route_start:
        best = None
        for w in workouts:
            ws = _utc(w["start_date"])
            if not ws:
                continue
            diff = abs((ws - route_start).total_seconds())
            if diff < 3600 and (best is None or diff < best[0]):
                best = (diff, w)
        if best:
            energy = best[1]["total_energy"]
            activity = (best[1]["activity_type"] or "").replace("HKWorkoutActivityType", "")

    return {
        "filename": route["filename"], "start_date": (route["start_date"] or "")[:10],
        "activity": activity, "distance_km": round(dist, 2), "elevation_gain_m": round(gain),
        "duration_min": round(dur_min, 1) if dur_min else None,
        "avg_speed_kmh": round(speed, 1) if speed else None,
        "energy_kcal": round(energy) if energy else None, "points": len(pts),
    }


def api_route_points(name, route_id=None, max_points=800):
    """A route's lat/lon, thinned so a map isn't handed 6,000 points. With no
    route_id, use the most recent route, so a map always has something to draw
    even before anyone picks one."""
    with open_db(name) as c:
        if not route_id:
            latest = c.execute(
                "SELECT id FROM routes ORDER BY start_date DESC LIMIT 1"
            ).fetchone()
            if not latest:
                return []
            route_id = latest["id"]
        total = c.execute(
            "SELECT COUNT(*) AS n FROM route_points WHERE route_id = ?", (route_id,)
        ).fetchone()["n"]
        step = max(1, total // max_points)
        rows = c.execute(
            "SELECT lat, lon FROM route_points WHERE route_id = ? AND rowid % ? = 0 ORDER BY rowid",
            (route_id, step),
        ).fetchall()
    return [{"lat": r["lat"], "lon": r["lon"]} for r in rows if r["lat"] is not None]


def import_status():
    with health() as c:
        return c.execute("SELECT * FROM import_status WHERE id = 1").fetchone()


def dashboard_stats():
    with health() as c:
        totals = c.execute(
            "SELECT COUNT(*) AS n, MIN(start_date) AS first, MAX(start_date) AS last FROM records"
        ).fetchone()
        types = c.execute(
            "SELECT type, COUNT(*) AS n, MIN(start_date) AS first, MAX(start_date) AS last "
            "FROM records GROUP BY type ORDER BY n DESC"
        ).fetchall()
        workouts = c.execute("SELECT COUNT(*) AS n FROM workouts").fetchone()["n"]
        summaries = c.execute("SELECT COUNT(*) AS n FROM activity_summary").fetchone()["n"]
        status = c.execute("SELECT * FROM import_status WHERE id = 1").fetchone()
    return {
        "totals": totals, "types": types, "workouts": workouts,
        "summaries": summaries, "status": status,
    }


def record_types():
    with health() as c:
        return [r["type"] for r in c.execute("SELECT DISTINCT type FROM records ORDER BY type")]


def browse(type_=None, start=None, end=None, limit=500):
    q = "SELECT type, source_name, unit, value, start_date, end_date FROM records WHERE 1 = 1"
    params = []
    if type_:
        q += " AND type = ?"
        params.append(type_)
    if start:
        q += " AND start_date >= ?"
        params.append(start)
    if end:
        q += " AND start_date <= ?"
        params.append(end)
    q += " ORDER BY start_date DESC LIMIT ?"
    params.append(limit)
    with health() as c:
        return c.execute(q, params).fetchall()


def _fmt(num, raw):
    if num is None:
        return raw or ""
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:.1f}"


# The day lives in the first 10 chars of start_date ("2026-07-20 09:00 +0200").
# SQLite's date() chokes on the trailing timezone, so we slice instead.
_AGG = {
    "avg": "AVG(value_num)", "sum": "SUM(value_num)",
    "min": "MIN(value_num)", "max": "MAX(value_num)", "count": "COUNT(*)",
}


def daily_rollup(type_, agg="avg", start=None, end=None):
    """One value per day for a record type. agg picks how the day's samples are
    combined: avg/min/max for things like heart rate, sum for step count and
    energy, count for how many samples landed that day."""
    expr = _AGG.get(agg, _AGG["avg"])
    where = "type = ?"
    params = [type_]
    if agg != "count":
        where += " AND value_num IS NOT NULL"
    if start:
        where += " AND start_date >= ?"
        params.append(start)
    if end:
        where += " AND start_date <= ?"
        params.append(end)
    q = (f"SELECT substr(start_date, 1, 10) AS day, {expr} AS v "
         f"FROM records WHERE {where} GROUP BY day ORDER BY day")
    with health() as c:
        return [(r["day"], r["v"]) for r in c.execute(q, params) if r["day"]]


def type_stats(type_):
    with health() as c:
        return c.execute(
            "SELECT COUNT(*) AS n, AVG(value_num) AS avg, MIN(value_num) AS min, "
            "MAX(value_num) AS max, SUM(value_num) AS sum, MAX(unit) AS unit, "
            "MIN(start_date) AS first, MAX(start_date) AS last "
            "FROM records WHERE type = ?",
            (type_,),
        ).fetchone()


# The handful of metrics worth showing at a glance, if the database has them.
_CARD_LATEST = [
    ("HKQuantityTypeIdentifierBodyMass", "Weight"),
    ("HKQuantityTypeIdentifierRestingHeartRate", "Resting HR"),
    ("HKQuantityTypeIdentifierHeartRate", "Heart rate"),
    ("HKQuantityTypeIdentifierBodyMassIndex", "BMI"),
    ("HKQuantityTypeIdentifierVO2Max", "VO2 max"),
]


def dashboard_cards():
    cards = []
    with health() as c:
        # Steps summed over the most recent day that has any.
        row = c.execute(
            "SELECT substr(start_date, 1, 10) AS day, SUM(value_num) AS s "
            "FROM records WHERE type = 'HKQuantityTypeIdentifierStepCount' "
            "AND value_num IS NOT NULL GROUP BY day ORDER BY day DESC LIMIT 1"
        ).fetchone()
        if row and row["day"]:
            cards.append({"label": "Steps", "value": str(int(row["s"])),
                          "unit": "", "when": row["day"]})
        for type_, label in _CARD_LATEST:
            r = c.execute(
                "SELECT value, value_num, unit, start_date FROM records "
                "WHERE type = ? ORDER BY start_date DESC LIMIT 1", (type_,)
            ).fetchone()
            if r:
                cards.append({"label": label, "value": _fmt(r["value_num"], r["value"]),
                              "unit": r["unit"] or "", "when": (r["start_date"] or "")[:10]})
    return cards


# ---------- routes + ECG ----------

def list_routes():
    with health() as c:
        return c.execute(
            "SELECT id, filename, start_date, point_count FROM routes ORDER BY start_date DESC"
        ).fetchall()


def get_route(route_id):
    with health() as c:
        route = c.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        pts = c.execute(
            "SELECT lat, lon, ele FROM route_points WHERE route_id = ? ORDER BY rowid",
            (route_id,),
        ).fetchall()
    return route, [(p["lat"], p["lon"]) for p in pts if p["lat"] is not None]


def list_ecg():
    with health() as c:
        return c.execute(
            "SELECT id, filename, recorded_date, classification, sample_rate, duration_s "
            "FROM ecg ORDER BY recorded_date DESC"
        ).fetchall()


def get_ecg(ecg_id, max_points=1200):
    """Return the ECG row plus its waveform, thinned to at most max_points so the
    browser isn't asked to draw 15,000 dots."""
    with health() as c:
        row = c.execute("SELECT * FROM ecg WHERE id = ?", (ecg_id,)).fetchone()
        total = c.execute(
            "SELECT COUNT(*) AS n FROM ecg_samples WHERE ecg_id = ?", (ecg_id,)
        ).fetchone()["n"]
        step = max(1, total // max_points)
        samples = [
            r["uv"] for r in c.execute(
                "SELECT idx, uv FROM ecg_samples WHERE ecg_id = ? AND idx % ? = 0 ORDER BY idx",
                (ecg_id, step),
            )
        ]
    return row, samples


def counts_extras():
    with health() as c:
        r = c.execute("SELECT COUNT(*) AS n FROM routes").fetchone()["n"]
        e = c.execute("SELECT COUNT(*) AS n FROM ecg").fetchone()["n"]
    return {"routes": r, "ecg": e}


def ingest_records(rows):
    """Merge a list of record dicts into the active database. Used by the push
    endpoint. Returns how many were actually new. Same INSERT OR IGNORE dedup as
    the XML import, so the phone can re-send overlapping windows safely."""
    cols = ("type", "source_name", "source_version", "device", "unit",
            "value", "value_num", "start_date", "end_date", "creation_date")
    tuples = []
    for r in rows:
        value = r.get("value")
        try:
            value_num = float(value)
        except (TypeError, ValueError):
            value_num = None
        tuples.append((
            r.get("type"), r.get("source_name"), r.get("source_version"),
            r.get("device"), r.get("unit"), None if value is None else str(value),
            value_num, r.get("start_date"), r.get("end_date"), r.get("creation_date"),
        ))
    with health() as c:
        before = c.total_changes
        c.executemany(
            f"INSERT OR IGNORE INTO records ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            tuples,
        )
        return c.total_changes - before
