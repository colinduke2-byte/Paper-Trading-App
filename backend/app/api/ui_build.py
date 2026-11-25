"""Is the bundled UI (frontend/dist) older than the UI sources next to it?

`npm run build` writes dist/build.json with a hash of frontend/src, index.html and package-lock.json
(frontend/scripts/build-info.mjs). When those sources are present, the server computes the same hash and,
if it differs, logs a warning and reports `ui_stale: true` in /api/status so the UI can say so. When the
sources are absent (a ZIP download run with run.bat, the Docker image) or build.json is missing, nothing
is reported: the check is silent and `ui_stale` is false.
"""
import hashlib
import json
import logging
import os
from typing import Optional

log = logging.getLogger("paperdesk")
ALGORITHM = "sha256-v1"
EXTRA_FILES = ("index.html", "package-lock.json")


def source_hash(frontend: str) -> Optional[str]:
    """The hash build-info.mjs computes, or None when the source tree is not there."""
    src = os.path.join(frontend, "src")
    if not os.path.isdir(src):
        return None
    files = []
    for dirpath, _, names in os.walk(src):
        files += [os.path.join(dirpath, n) for n in names]
    files += [p for p in (os.path.join(frontend, f) for f in EXTRA_FILES) if os.path.isfile(p)]
    rels = sorted(os.path.relpath(p, frontend).replace(os.sep, "/") for p in files)
    h = hashlib.sha256()
    for rel in rels:
        with open(os.path.join(frontend, rel), "rb") as f:
            body = f.read().replace(b"\r\n", b"\n")
        h.update(f"{rel}\0{hashlib.sha256(body).hexdigest()}\n".encode())
    return h.hexdigest()


def check(dist: str) -> bool:
    """True when dist/build.json names a different source hash than the sources beside it."""
    try:
        with open(os.path.join(dist, "build.json"), encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(info, dict) or info.get("algorithm") != ALGORITHM:
        return False
    current = source_hash(os.path.join(dist, ".."))
    if current is None or current == info.get("source_hash"):
        return False
    log.warning("The bundled UI in %s was built from older sources than frontend/src. "
                "Run 'npm run build' in frontend/ to rebuild it.", os.path.abspath(dist))
    return True
