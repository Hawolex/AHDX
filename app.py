"""
AHDX web app.

Small Flask front end over the storage layer: a page to import an Apple Health
export, a dashboard, a browser for the raw records, database management, and a
JSON endpoint the phone can push to. All local, no accounts, no outbound calls.
"""
import base64
import csv
import io
import os
import secrets
import threading
import urllib.request

from flask import (
    Flask, Response, abort, flash, jsonify, redirect, render_template, request,
    session, url_for,
)
from werkzeug.utils import secure_filename

import db
import jobs
import scanner


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# Auth ships ON. AHDX keeps its own salted password in the registry DB, set once
# via the GUI on first run. When you set it, AHDX also pushes it to Grafana (see
# _sync_grafana_password) so one password covers both. The /ingest push endpoint
# stays open; it's for the LAN.
AUTH_ENABLED = _truthy(os.environ.get("AHDX_AUTH", "true"))
GRAFANA_URL = os.environ.get("AHDX_GRAFANA_URL", "http://ahdx-grafana:3000").rstrip("/")


def _has_password():
    return db.has_password()


def _check_password(pw):
    return db.check_password(pw)


def _sync_grafana_password(new_pw):
    """Best-effort: set Grafana's admin password to match. A fresh Grafana ships
    as admin/admin, so we change it from there on first setup; on later changes
    we try the new password as the "current" one too. Failure is ignored — AHDX
    still works, Grafana just keeps whatever it had."""
    import json as _json
    for current in ("admin", new_pw):
        try:
            auth = base64.b64encode(f"admin:{current}".encode()).decode()
            body = _json.dumps({"oldPassword": current, "newPassword": new_pw}).encode()
            req = urllib.request.Request(
                GRAFANA_URL + "/api/user/password", data=body, method="PUT",
                headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            continue
    return False

# Optional shared key for the read API. If set, callers (Grafana, scripts) must
# send it as the X-Api-Key header or a ?key= query param. If unset, the API is
# open on the LAN, same as /ingest.
API_KEY = os.environ.get("AHDX_API_KEY") or None

app = Flask(__name__)

with app.app_context():
    db.init()

# A stable session secret. Prefer the env var; otherwise keep a random one in the
# settings table so logins survive a restart without hard-coding anything.
_secret = os.environ.get("SECRET_KEY") or db.get_setting("secret_key")
if not _secret:
    _secret = secrets.token_hex(32)
    db.set_setting("secret_key", _secret)
app.config["SECRET_KEY"] = _secret

scanner.start()

# Endpoints reachable without a login: the login/setup pages themselves, the
# health check, static files, and the machine-to-machine push endpoint.
OPEN_ENDPOINTS = {"login", "setup", "health", "static", "ingest"}


@app.before_request
def require_login():
    # The read API is machine-to-machine: it has its own key, so the browser
    # login never applies to it.
    if request.path.startswith("/api"):
        return
    if not AUTH_ENABLED or session.get("authed") or request.endpoint in OPEN_ENDPOINTS:
        return
    # First run with auth on and no password yet: force setting one.
    return redirect(url_for("setup" if not _has_password() else "login"))


def _api_guard():
    """Return an error response if the API key is required and missing/wrong,
    else None."""
    if not API_KEY:
        return None
    given = request.headers.get("X-Api-Key") or request.args.get("key")
    if given != API_KEY:
        return jsonify({"error": "unauthorized: send the X-Api-Key header"}), 401
    return None


@app.context_processor
def inject_globals():
    # Every page shows the database picker, so hand it the list each render.
    return {
        "databases": db.list_databases(),
        "active_db": db.get_active(),
        "auth_enabled": AUTH_ENABLED,
    }


# ---------- auth (only active when AHDX_AUTH is on) ----------

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if not AUTH_ENABLED or _has_password():
        return redirect(url_for("index"))
    if request.method == "POST":
        pw = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(pw) < 6:
            flash("Use at least 6 characters.", "error")
        elif pw != confirm:
            flash("The two passwords don't match.", "error")
        else:
            db.set_password(pw)
            _sync_grafana_password(pw)   # make it the Grafana admin password too
            session["authed"] = True
            flash("Password set — it's now the login for both AHDX and Grafana.", "success")
            return redirect(url_for("index"))
    return render_template("auth.html", mode="setup")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED:
        return redirect(url_for("index"))
    if not _has_password():
        return redirect(url_for("setup"))
    if request.method == "POST":
        if _check_password(request.form.get("password", "")):
            session["authed"] = True
            return redirect(url_for("index"))
        flash("Wrong password.", "error")
    return render_template("auth.html", mode="login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login") if AUTH_ENABLED else url_for("index"))


@app.route("/health")
def health():
    return {"ok": True}


# ---------- import ----------

@app.route("/")
def index():
    return render_template("import.html", status=db.import_status())


@app.route("/import", methods=["POST"])
def do_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Pick your Apple Health export first.", "error")
        return redirect(url_for("index"))
    if jobs.import_running():
        flash("An import is already running. Give it a moment.", "error")
        return redirect(url_for("index"))

    saved = os.path.join(db.UPLOAD_DIR, "upload_" + secure_filename(f.filename))
    f.save(saved)
    # Parse off the request thread; the page polls /import/status for progress.
    threading.Thread(
        target=jobs.run_import, args=(saved, "upload:" + f.filename), daemon=True
    ).start()
    flash("Import started.", "success")
    return redirect(url_for("index"))


@app.route("/import/status")
def import_status():
    s = db.import_status()
    return jsonify({k: s[k] for k in s.keys()})


# ---------- dashboard + browse ----------

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html", cards=db.dashboard_cards(), **db.dashboard_stats())


@app.route("/trends")
def trends():
    types = db.record_types()
    sel = request.args.get("type") or (types[0] if types else None)
    agg = request.args.get("agg", "avg")
    if agg not in ("avg", "sum", "min", "max", "count"):
        agg = "avg"
    points = db.daily_rollup(sel, agg) if sel else []
    stats = db.type_stats(sel) if sel else None
    return render_template(
        "trends.html", types=types, sel_type=sel or "", agg=agg,
        points=points, stats=stats,
    )


@app.route("/browse")
def browse():
    type_ = request.args.get("type") or None
    start = request.args.get("start") or None
    end = request.args.get("end") or None
    rows = db.browse(type_, start, end, limit=500)
    return render_template(
        "browse.html", rows=rows, types=db.record_types(),
        sel_type=type_ or "", start=start or "", end=end or "",
    )


@app.route("/export.csv")
def export_csv():
    type_ = request.args.get("type") or None
    start = request.args.get("start") or None
    end = request.args.get("end") or None
    rows = db.browse(type_, start, end, limit=5_000_000)

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["type", "source_name", "unit", "value", "start_date", "end_date"])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)
        for r in rows:
            writer.writerow([r["type"], r["source_name"], r["unit"], r["value"],
                             r["start_date"], r["end_date"]])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    return Response(
        generate(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=ahdx-records.csv"},
    )


# ---------- routes + ECG ----------

@app.route("/routes")
def routes():
    return render_template("routes.html", routes=db.list_routes())


@app.route("/routes/<int:route_id>")
def route_detail(route_id):
    route, points = db.get_route(route_id)
    if not route:
        abort(404)
    return render_template("route_detail.html", route=route, points=points)


@app.route("/ecg")
def ecg():
    return render_template("ecg.html", items=db.list_ecg())


@app.route("/ecg/<int:ecg_id>")
def ecg_detail(ecg_id):
    row, samples = db.get_ecg(ecg_id)
    if not row:
        abort(404)
    return render_template("ecg_detail.html", ecg=row, samples=samples)


# ---------- databases ----------

@app.route("/databases")
def databases():
    return render_template("databases.html", overview=db.database_overview())


@app.route("/databases/new", methods=["POST"])
def new_database():
    try:
        db.create_database(request.form.get("name", ""))
        flash("Database created and set active.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("databases"))


@app.route("/databases/<int:db_id>/rename", methods=["POST"])
def rename_database(db_id):
    try:
        db.rename_database(db_id, request.form.get("name", ""))
        flash("Database renamed.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("databases"))


@app.route("/databases/<int:db_id>/activate", methods=["POST"])
def activate_database(db_id):
    db.set_active(db_id)
    flash("Switched active database.", "success")
    return redirect(request.referrer or url_for("databases"))


@app.route("/databases/<int:db_id>/delete", methods=["POST"])
def delete_database(db_id):
    try:
        db.delete_database(db_id)
        flash("Database deleted.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("databases"))


# ---------- push endpoint for iOS Shortcuts ----------

@app.route("/ingest", methods=["POST"])
def ingest():
    """Accept a batch of records as JSON and merge them into the active database.
    Body is either a list of record objects or {"records": [...]}. A record is
    {type, value, unit, start_date, end_date, source_name}. Same dedup as the
    XML import, so re-sending an overlapping window is harmless."""
    data = request.get_json(silent=True)
    rows = data.get("records") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return jsonify({"error": 'send a JSON list, or {"records": [...]}'}), 400
    added = db.ingest_records(rows)
    return jsonify({"received": len(rows), "added": added})


# ---------- read API (for Grafana, scripts, etc.) ----------

@app.route("/api")
def api_index():
    """The menu. Lists the databases and every endpoint so you can discover the
    API by opening this one URL."""
    guard = _api_guard()
    if guard:
        return guard
    base = request.host_url.rstrip("/")
    names = [d["name"] for d in db.list_databases()]
    example_db = names[0] if names else "Steffen"
    return jsonify({
        "app": "AHDX read API",
        "databases": names,
        "how": "Add ?db=<name> to target a database. Leave it off to use the active one.",
        "auth": "open" if not API_KEY else "send X-Api-Key header (or ?key=)",
        "endpoints": [
            {"path": "/api/databases", "returns": "the list of databases"},
            {"path": "/api/types", "params": ["db"], "returns": "record types with counts"},
            {"path": "/api/records", "params": ["db", "type", "from", "to", "limit"],
             "returns": "raw records"},
            {"path": "/api/daily", "params": ["db", "type", "agg", "from", "to"],
             "returns": "one value per day; agg is avg|sum|min|max|count. Best for Grafana."},
            {"path": "/api/workouts", "params": ["db"], "returns": "workouts"},
            {"path": "/api/activity", "params": ["db"], "returns": "daily activity summaries"},
            {"path": "/api/sleep", "params": ["db", "from", "to"],
             "returns": "one row per night: hours per stage + a 0-100 score"},
            {"path": "/api/routes", "params": ["db"], "returns": "GPS routes list"},
            {"path": "/api/route", "params": ["db", "id"], "returns": "one route's lat/lon points"},
        ],
        "examples": [
            f"{base}/api/types?db={example_db}",
            f"{base}/api/daily?db={example_db}&type=HKQuantityTypeIdentifierStepCount&agg=sum",
            f"{base}/api/daily?db={example_db}&type=HKQuantityTypeIdentifierHeartRate&agg=avg&from=2026-01-01",
        ],
    })


@app.route("/api/databases")
def api_databases():
    guard = _api_guard()
    if guard:
        return guard
    return jsonify([
        {"name": d["name"], "file": d["filename"], "active": bool(d["is_active"])}
        for d in db.list_databases()
    ])


def _with_db(fn):
    """Run a db.api_* call, turning an unknown ?db= into a clean 404."""
    guard = _api_guard()
    if guard:
        return guard
    try:
        return jsonify(fn())
    except KeyError:
        return jsonify({"error": "unknown database; see /api/databases"}), 404


@app.route("/api/types")
def api_types():
    return _with_db(lambda: db.api_types(request.args.get("db")))


@app.route("/api/records")
def api_records():
    type_ = request.args.get("type")
    if not type_:
        return jsonify({"error": "type is required; see /api/types"}), 400
    limit = min(int(request.args.get("limit", 10000)), 200000)
    return _with_db(lambda: db.api_records(
        request.args.get("db"), type_, request.args.get("from"),
        request.args.get("to"), limit,
    ))


@app.route("/api/daily")
def api_daily():
    type_ = request.args.get("type")
    if not type_:
        return jsonify({"error": "type is required; see /api/types"}), 400
    agg = request.args.get("agg", "avg")
    if agg not in ("avg", "sum", "min", "max", "count"):
        agg = "avg"
    return _with_db(lambda: db.api_daily(
        request.args.get("db"), type_, agg,
        request.args.get("from"), request.args.get("to"),
    ))


@app.route("/api/workouts")
def api_workouts():
    return _with_db(lambda: db.api_workouts(request.args.get("db")))


@app.route("/api/activity")
def api_activity():
    return _with_db(lambda: db.api_activity(request.args.get("db")))


@app.route("/api/sleep")
def api_sleep():
    return _with_db(lambda: db.api_sleep(
        request.args.get("db"), request.args.get("from"), request.args.get("to")))


@app.route("/api/routes")
def api_routes():
    return _with_db(lambda: db.api_routes(request.args.get("db")))


@app.route("/api/route")
def api_route():
    # Missing / empty / unresolved-variable id -> latest route (handled in db).
    rid = request.args.get("id")
    rid = int(rid) if (rid and rid.isdigit()) else None
    return _with_db(lambda: db.api_route_points(request.args.get("db"), rid))


@app.route("/api/route/stats")
def api_route_stats():
    rid = request.args.get("id")
    rid = int(rid) if (rid and rid.isdigit()) else None
    return _with_db(lambda: db.api_route_stats(request.args.get("db"), rid))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
