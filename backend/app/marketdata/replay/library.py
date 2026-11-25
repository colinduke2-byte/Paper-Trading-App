"""Find session files: the bundled samples plus any the user recorded into their data directory."""
import os
from typing import Optional

from .format import SUFFIX, SessionFormatError, read_header, session_id
from ...core.errors import NotFound

BUNDLED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sessions")


def session_dirs(data_dir: Optional[str] = None) -> list:
    dirs = [BUNDLED_DIR]
    if data_dir:
        dirs.append(os.path.join(data_dir, "sessions"))
    return dirs


def list_sessions(dirs: list) -> list:
    """One row per readable session file, sorted by date. Unreadable files are listed with their error."""
    rows, seen = [], set()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(SUFFIX):
                continue
            path = os.path.join(d, name)
            sid = session_id(path)
            if sid in seen:
                continue
            seen.add(sid)
            row = {"id": sid, "bundled": os.path.abspath(d) == BUNDLED_DIR, "size_bytes": os.path.getsize(path)}
            try:
                h = read_header(path)
            except SessionFormatError as e:
                rows.append({**row, "error": str(e), "label": name, "date": "", "symbols": []})
                continue
            rows.append({**row, "label": h.get("label") or sid, "description": h.get("description", ""),
                         "date": h["date"], "start": h["start"], "end": h.get("end"),
                         "source": h.get("source", ""), "simulated": bool(h.get("simulated", True)),
                         "symbols": [s["symbol"] for s in h["symbols"]], "version": h["version"]})
    rows.sort(key=lambda r: (r.get("date") or "", r["id"]))
    return rows


def find_session(dirs: list, sid: str) -> str:
    if not sid or os.path.basename(sid) != sid or sid.startswith("."):
        raise NotFound(f"no session named {sid!r}")
    for d in dirs:
        path = os.path.join(d, sid + SUFFIX)
        if os.path.isfile(path):
            return path
    raise NotFound(f"no session named {sid!r}")
