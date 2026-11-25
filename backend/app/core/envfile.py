"""The optional .env settings file: one parser and one loader for every setting PaperDesk documents.

Which file: PAPERDESK_ENV_FILE when it is set (an empty value means "no file"), otherwise .env in the
project folder (next to run.sh). Values in the file never override variables already set in the
environment. Every entry point (the server factory, the run scripts' preflight, the Alpaca check)
loads it first, before reading any setting.
"""
import os
import re
from pathlib import Path
from typing import Optional


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def read_env_file(path) -> dict:
    """Parse KEY=VALUE lines like docker compose does for the simple cases: '#' comments, optional
    'export ', single or double quotes (taken literally), and ' #' inline comments on unquoted values."""
    out = {}
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if key:
            out[key] = value
    return out


def env_file_path(environ=None) -> Optional[Path]:
    environ = os.environ if environ is None else environ
    if "PAPERDESK_ENV_FILE" in environ:
        p = environ.get("PAPERDESK_ENV_FILE") or ""
        return Path(p) if p.strip() else None
    return project_root() / ".env"


def load_into(path, environ=None) -> list:
    """Copy the file's settings into ``environ`` without overriding names already set.
    Returns the names it set (never the values). A missing file does nothing."""
    environ = os.environ if environ is None else environ
    if path is None or not Path(path).is_file():
        return []
    names = []
    for k, v in read_env_file(path).items():
        if k not in environ:
            environ[k] = v
            names.append(k)
    return names


_loaded = False


def load_settings(environ=None) -> list:
    """Load the settings file into the process environment once (later calls do nothing)."""
    global _loaded
    if environ is None:
        if _loaded:
            return []
        _loaded = True
    return load_into(env_file_path(environ), environ)
