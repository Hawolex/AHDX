-- Schema for one Apple Health database.
--
-- Apple's export.xml is flat: a big pile of <Record> rows plus some <Workout>
-- and <ActivitySummary> rows. We keep that shape instead of splitting the
-- hundreds of health "types" into their own tables, because Apple adds new
-- types with every iOS release and we don't want a migration each time.
--
-- Records have no ID from Apple, so we build our own uniqueness key and use
-- INSERT OR IGNORE on import. That way re-scanning the same export is a no-op
-- and a newer export only adds the rows we haven't seen.

CREATE TABLE IF NOT EXISTS records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    type           TEXT NOT NULL,      -- e.g. HKQuantityTypeIdentifierHeartRate
    source_name    TEXT,
    source_version TEXT,
    device         TEXT,
    unit           TEXT,
    value          TEXT,               -- raw value straight from the file
    value_num      REAL,               -- the same value as a number, or NULL if it isn't one
    start_date     TEXT,
    end_date       TEXT,
    creation_date  TEXT
);

-- The natural key. COALESCE keeps NULLs from slipping past the uniqueness check
-- (in SQLite two NULLs are treated as distinct, which would let duplicates in).
CREATE UNIQUE INDEX IF NOT EXISTS uq_records ON records (
    type,
    COALESCE(start_date, ''),
    COALESCE(end_date, ''),
    COALESCE(value, ''),
    COALESCE(source_name, '')
);
CREATE INDEX IF NOT EXISTS idx_records_type_start ON records (type, start_date);

CREATE TABLE IF NOT EXISTS workouts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_type  TEXT,
    duration       REAL,
    duration_unit  TEXT,
    total_distance REAL,
    distance_unit  TEXT,
    total_energy   REAL,
    energy_unit    TEXT,
    start_date     TEXT,
    end_date       TEXT,
    source_name    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_workouts ON workouts (
    COALESCE(activity_type, ''),
    COALESCE(start_date, ''),
    COALESCE(end_date, ''),
    COALESCE(source_name, '')
);

CREATE TABLE IF NOT EXISTS activity_summary (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    date               TEXT UNIQUE,   -- one summary per day
    active_energy      REAL,
    active_energy_goal REAL,
    move_time          REAL,
    move_time_goal     REAL,
    exercise_time      REAL,
    exercise_time_goal REAL,
    stand_hours        REAL,
    stand_hours_goal   REAL
);

-- GPS tracks from outdoor workouts (the workout-routes/*.gpx files in the zip).
CREATE TABLE IF NOT EXISTS routes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT UNIQUE,
    start_date  TEXT,
    point_count INTEGER
);
CREATE TABLE IF NOT EXISTS route_points (
    route_id INTEGER NOT NULL REFERENCES routes(id),
    lat      REAL,
    lon      REAL,
    ele      REAL,
    t        TEXT
);
CREATE INDEX IF NOT EXISTS idx_route_points ON route_points(route_id);

-- ECG readings (the electrocardiograms/*.csv files). Each is a short waveform
-- plus some header lines; we keep the metadata we can read and all the samples.
CREATE TABLE IF NOT EXISTS ecg (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    filename       TEXT UNIQUE,
    recorded_date  TEXT,
    classification TEXT,
    sample_rate    REAL,
    duration_s     REAL,
    unit           TEXT
);
CREATE TABLE IF NOT EXISTS ecg_samples (
    ecg_id INTEGER NOT NULL REFERENCES ecg(id),
    idx    INTEGER,
    uv     REAL
);
CREATE INDEX IF NOT EXISTS idx_ecg_samples ON ecg_samples(ecg_id);

-- One row, updated while an import runs so the web page can show progress.
CREATE TABLE IF NOT EXISTS import_status (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    state        TEXT,                -- idle | running | done | error
    records_seen INTEGER DEFAULT 0,   -- rows read from the file
    records_new  INTEGER DEFAULT 0,   -- rows that were actually new
    current_type TEXT,
    source       TEXT,                -- what triggered it: upload, inbox filename, or ingest
    message      TEXT,
    started_at   TEXT,
    finished_at  TEXT
);
INSERT OR IGNORE INTO import_status (id, state) VALUES (1, 'idle');
