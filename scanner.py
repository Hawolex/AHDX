"""
Background watcher for the inbox folder.

Every AHDX_SCAN_INTERVAL seconds it looks in data/inbox/ for a new export.zip or
export.xml, merges it into the active database, and moves the file to
data/inbox/done/ so it isn't picked up again. Drop exports there by hand or point
a synced folder at it, and imports happen without anyone clicking a button.

Set AHDX_SCAN_INTERVAL to 0 to turn the watcher off.
"""
import os
import threading
import time

import db
import jobs

DEFAULT_INTERVAL = 1800  # 30 minutes


def start():
    interval = int(os.environ.get("AHDX_SCAN_INTERVAL", DEFAULT_INTERVAL))
    if interval <= 0:
        return
    threading.Thread(target=_loop, args=(interval,), daemon=True).start()


def _loop(interval):
    while True:
        try:
            scan_once()
        except Exception:
            # A bad file or a locked database shouldn't kill the watcher; it'll
            # try again next tick.
            pass
        time.sleep(interval)


def scan_once():
    """Import every pending file, oldest name first. Returns how many it took."""
    done = 0
    for path in _pending():
        if jobs.run_import(path, source="inbox:" + os.path.basename(path)):
            _move_to_done(path)
            done += 1
    return done


def _pending():
    files = []
    for name in sorted(os.listdir(db.INBOX_DIR)):
        p = os.path.join(db.INBOX_DIR, name)
        if os.path.isfile(p) and name.lower().endswith((".zip", ".xml")):
            files.append(p)
    return files


def _move_to_done(path):
    name = os.path.basename(path)
    dest = os.path.join(db.INBOX_DONE, name)
    # Don't clobber an earlier file of the same name.
    n = 1
    stem, ext = os.path.splitext(name)
    while os.path.exists(dest):
        dest = os.path.join(db.INBOX_DONE, f"{stem}-{n}{ext}")
        n += 1
    os.replace(path, dest)
