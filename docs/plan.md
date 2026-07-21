# AHDX build plan

AHDX is a small self-hosted app for pulling your own Apple Health data out of
Apple's export and into a SQLite database you control. It runs in Docker, works
on Windows through Docker Desktop, and has a web page for loading the export and
looking through it. Everything stays inside the container. Nothing is sent
anywhere.

AHDX is short for Apple Health Data eXporter.

## Why it exists

You're in the EU, so the health data Apple holds about you is yours to take and
keep. Apple will export it, but what you get back is one enormous XML file
that's awkward to use. This app turns that file into an ordinary SQLite
database and gives you a browser page to import and query it. No account, no
cloud, no telemetry.

## What Apple actually gives you

On the iPhone: Health app, tap your profile picture, "Export All Health Data".
You get `export.zip`. Inside it:

- `apple_health_export/export.xml` is the main file and the only one we need to
  start. It holds `<Record>` entries (heart rate, steps, weight, sleep, etc.),
  `<Workout>` entries, and `<ActivitySummary>` rows (one per day). This file
  gets big: real exports run from tens of megabytes to a few gigabytes.
- `workout-routes/*.gpx` are GPS tracks for outdoor workouts.
- `electrocardiograms/*.csv` are ECG readings.
- `export_cda.xml` is a clinical-format copy of the same data. We skip it.

The size is the one real problem to solve. We can't load the whole XML into
memory, so we stream it.

## Stack

Same shape as the other self-hosted apps here, because it works and there's
little to break:

- Python + Flask, server-rendered pages, one CSS file. No frontend framework.
- SQLite via the stdlib `sqlite3`. No ORM.
- Docker + docker-compose. One service, one volume for the databases and uploads.
- XML parsing with `xml.etree.ElementTree.iterparse`. It reads the file element
  by element and lets us throw each one away after we've stored it, so memory
  stays flat no matter how large the export is.

If we add charts, they get drawn in the browser from data the page already has,
with the chart library shipped inside the image. No CDN, so it still works with
the machine offline.

## Data model, first cut

Three tables cover nearly everything in export.xml:

- `records`: one row per measurement. Columns: type, source_name, source_version,
  device, unit, value, value_num, start_date, end_date, creation_date. Index on
  (type, start_date).
- `workouts`: activity_type, duration, duration_unit, total_distance,
  distance_unit, total_energy, energy_unit, start_date, end_date, source_name.
- `activity_summary`: date, active_energy_burned, active_energy_goal, move_time,
  exercise_time, stand_hours.

`value` keeps the raw text from the file. `value_num` holds the same thing as a
REAL when it parses as a number, so numeric types (weight, heart rate) chart
cleanly while text types (like sleep state) still survive the round trip.

Later, if you want them: workout GPS points, ECG samples, and the metadata
key/value pairs Apple attaches to some records.

## The "add a database" part

You said standard SQLite, with a way to add databases. Here's what I'd build.
Tell me if you meant something different.

The volume has a `databases/` folder. Each health database is one `.db` file in
there. A small `registry.db` tracks the list and which one is active. The UI
gives you:

- a picker to switch the active database,
- "New database": name it, the app creates an empty `.db` and switches to it,
- import always writes into whichever database is active.

That handles the cases I'd expect: one database per person in the house, a fresh
one each year, or a scratch database to test an import before you trust it.

## Pages

- `/` import. Drop `export.zip` or `export.xml`. It parses into the active
  database and shows a live count while it runs.
- `/dashboard` what's in here: record count per type, the date range covered,
  number of workouts, last import time.
- `/browse` pick a type and a date range, read the rows, download them as CSV.
- `/databases` list, switch, create, delete.
- a status endpoint for Docker's health check.

## Import, the tricky bit

A big export takes a while to parse, so import can't block a request until it
finishes. I'd run the parse in a background thread and write progress (rows
seen, current record type) to a status row that the import page polls. That's
enough for a single-user local app; a real job queue would be overkill.

Inserts go in batches, committing every few thousand rows, and each XML element
gets cleared right after we read it.

Re-importing is a merge, not a reload. Every export holds your whole history, so
loading a newer one shouldn't duplicate the old rows. Apple gives records no
stable ID, so we make our own key: a UNIQUE index on
(type, start_date, end_date, value, source_name) with `INSERT OR IGNORE`. Load
the same export twice and nothing changes; load a fresher one and only the new
rows land. Workouts key on (activity_type, start_date, end_date, source_name),
activity summaries on the date. This is what keeps "the latest" flowing into the
database without a wipe.

## Keeping it current (the interval scan)

You asked for a periodic scan that pulls recent data, free, with nothing behind
a paid tier. The limit to be honest about: Apple has no free server-side API. A
container on your PC can't reach into the phone and pull Health data by itself.
The full `export.xml` is a manual export from the Health app. So the "interval
scan" watches for exports rather than fetching them, and there's a push path for
the automatable part.

Two free ways to feed it, both landing in the same merge:

1. Watched inbox. AHDX checks `data/inbox/` on a timer (default every 30
   minutes, set by `AHDX_SCAN_INTERVAL`). Drop an `export.zip` or `export.xml`
   in there, or point a synced folder at it, and the container ingests it on its
   own and moves the file to `data/inbox/done/`. No clicking.
2. Push endpoint for iOS Shortcuts. A free Shortcut automation on the phone can
   read recent metrics (steps, heart rate, weight, sleep) on a schedule and POST
   them as JSON to `/ingest`. Built into iOS, no third-party app, no paid tier.
   It won't carry full history the way the manual export does, but it keeps
   recent data arriving hands-free.

What we can't do for free: a fully automatic full-history export. Apple only
does that by hand. Automatic recent data is fine through the Shortcut push. The
README will spell out both, with the exact Shortcut steps.

## Build order

1. Skeleton. Dockerfile, compose, a Flask app that boots, empty pages, status
   endpoint. Confirm it runs on Windows via Docker Desktop and the page loads at
   http://localhost:PORT.
2. Parser and import. Streaming XML into the three tables, background parse with
   progress. This is the core. Get it solid against a real export.
3. Browse and dashboard. Counts, filtering, CSV export.
4. Multiple databases. Registry, switch, create, delete.
5. Release polish. A README with the export steps and screenshots, a license, a
   small sample export.xml so people can try it, and a plain statement that the
   app makes no network calls.

## Open-source prep (later)

No git repo yet, per your note. When we set one up:

- License: MIT if you want the widest reuse, AGPL if you want anyone who runs a
  modified hosted copy to publish their changes. Your call.
- README that leads with "your data, your machine" and the export steps.
- No analytics and no outbound requests, stated plainly, since this is health data.

## What I need from you

1. Does AHDX stand for Apple Health Data eXport, or something else?
2. Import scope to start: just `export.xml`, or accept the whole `export.zip` and
   pull the XML out ourselves? The zip is friendlier for you.
3. "Add a database": is the registry idea above what you meant, or did you mean
   attaching an existing external `.db` file, or room for other engines (Postgres)
   down the line?
4. A port preference. I'd default to 8080.
5. If you can drop a real `export.xml` (or a trimmed chunk of one) into the
   project, I'll build the parser against actual data. Apple's XML has quirks
   that only show up in a real file.
