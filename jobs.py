"""
One place that runs an import, shared by the upload page and the inbox scanner
so the two can't parse into the same database at once. A plain lock is enough:
this is a single-user app, imports are rare, and a second one can just wait.
"""
import os
import shutil
import threading
import zipfile

import db
import parser

_LOCK = threading.Lock()


def import_running():
    if _LOCK.acquire(blocking=False):
        _LOCK.release()
        return False
    return True


def extract_xml(path, workdir):
    """Return a path to the main export XML. If path is Apple's export zip, pull
    the XML out of it.

    Apple localizes the filename to the phone's language, so it is not always
    'export.xml' (a Norwegian phone writes 'eksport.xml', German 'exportieren',
    and so on). The one steady name is the clinical copy 'export_cda.xml', which
    stays the same in every language and holds the same data in a format we don't
    use. So we take the largest .xml that isn't the _cda one."""
    if not zipfile.is_zipfile(path):
        return path  # already an XML, or something we'll fail on cleanly

    with zipfile.ZipFile(path) as z:
        xmls = [n for n in z.namelist() if n.lower().endswith(".xml")]
        main = [n for n in xmls if not n.rsplit("/", 1)[-1].lower().endswith("_cda.xml")]
        if not main:
            return None
        member = max(main, key=lambda n: z.getinfo(n).file_size)
        out = os.path.join(workdir, "export.xml")
        with z.open(member) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        return out


def run_import(file_path, source):
    """Extract if needed, then merge into the active database. Blocks until done.
    Returns False if another import already holds the lock."""
    if not _LOCK.acquire(blocking=False):
        return False
    try:
        xml_path = extract_xml(file_path, db.UPLOAD_DIR)
        if not xml_path:
            with db.health() as c:
                c.execute(
                    "UPDATE import_status SET state = 'error', "
                    "message = 'No export.xml found in that file.' WHERE id = 1"
                )
            return False
        parser.parse(xml_path, db.active_path(), source=source)
        # GPS routes and ECG readings live only in the zip, so pull them too.
        if zipfile.is_zipfile(file_path):
            parser.parse_extras(file_path, db.active_path())
        return True
    finally:
        _LOCK.release()
