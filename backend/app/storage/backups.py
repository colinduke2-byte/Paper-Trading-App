"""Automatic rotating backups in <data>/backups/, plus snapshots taken before destructive actions.

Every backup is an archive in the format of archive.py, written atomically and then verified by reading it back
in full (checksums, migration, validation, ledger invariants); a backup that fails verification is deleted and
the failure reported. Pruning runs only after a new automatic backup verified, only touches files whose names
this module wrote, and keeps:
  * the newest automatic backup of each of the last KEEP_HOURLY hours that have one (default 24),
  * the newest automatic backup of each of the last KEEP_DAILY days that have one (default 14),
  * the newest KEEP_EVENTS snapshots taken before resets, deletes, restores, imports and migrations (default 20),
  * manual snapshots until the user deletes them.

Settings (environment): PAPERDESK_BACKUP_INTERVAL seconds between automatic backups (default 3600; 0 turns
them off), PAPERDESK_BACKUP_KEEP_HOURLY, PAPERDESK_BACKUP_KEEP_DAILY, PAPERDESK_BACKUP_KEEP_EVENTS.
Times are wall-clock (injected as ``wall``): backups are about real elapsed time, not the simulated market.
"""
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from ..core.errors import NotFound  # a KeyError: callers that catch KeyError still do
from . import archive, fsutil

DIR = "backups"
KINDS = ("auto", "manual", "pre-reset", "pre-delete", "pre-restore", "pre-import", "pre-migration", "pre-recovery")
EVENT_KINDS = ("pre-reset", "pre-delete", "pre-restore", "pre-import", "pre-migration", "pre-recovery")
_NAME = re.compile(r"^paperdesk-(?P<kind>[a-z-]+?)-(?P<stamp>\d{8}T\d{6}Z)(?:-(?P<n>\d{1,3}))?\.zip$")


def _int_env(name: str, default: int, lo: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, not {raw!r}") from None
    if v < lo:
        raise ValueError(f"{name} must be at least {lo}")
    return v


class BackupError(RuntimeError):
    pass


class BackupManager:
    def __init__(self, data_dir: str, wall: Callable[[], float] = time.time, interval: Optional[int] = None,
                 keep_hourly: Optional[int] = None, keep_daily: Optional[int] = None, keep_events: Optional[int] = None):
        self.data_dir = data_dir
        self.dir = os.path.join(data_dir, DIR)
        self.wall = wall
        self.interval = _int_env("PAPERDESK_BACKUP_INTERVAL", 3600) if interval is None else interval
        self.keep_hourly = _int_env("PAPERDESK_BACKUP_KEEP_HOURLY", 24, 1) if keep_hourly is None else keep_hourly
        self.keep_daily = _int_env("PAPERDESK_BACKUP_KEEP_DAILY", 14, 1) if keep_daily is None else keep_daily
        self.keep_events = _int_env("PAPERDESK_BACKUP_KEEP_EVENTS", 20, 1) if keep_events is None else keep_events
        self.lock = threading.Lock()
        self.last_error: Optional[str] = None
        self.last_auto: Optional[float] = None

    # ---- names ----------------------------------------------------------------------------------
    @staticmethod
    def parse(name: str) -> Optional[dict]:
        m = _NAME.match(name)
        if not m or m.group("kind") not in KINDS:
            return None
        ts = datetime.strptime(m.group("stamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
        return {"name": name, "kind": m.group("kind"), "created_ts": ts, "created": fsutil.iso(ts)}

    def path_of(self, name: str) -> str:
        """The path of a backup by name; refuses anything that is not one of ours (no path tricks)."""
        if self.parse(name) is None:
            raise NotFound(f"There is no backup named {name[:80]}.")
        p = os.path.join(self.dir, name)
        if not os.path.isfile(p):
            raise NotFound(f"There is no backup named {name[:80]}.")
        return p

    def list(self) -> list:
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        rows = []
        for n in names:
            info = self.parse(n)
            if info is None:
                continue
            try:
                info["size"] = os.path.getsize(os.path.join(self.dir, n))
            except OSError:
                continue
            rows.append(info)
        rows.sort(key=lambda r: (r["created_ts"], r["name"]), reverse=True)
        return rows

    # ---- creating ---------------------------------------------------------------------------------
    def create(self, kind: str, doc: Optional[dict] = None, market: Optional[dict] = None, info: Optional[dict] = None,
               raw: Optional[dict] = None, sessions: Optional[dict] = None) -> dict:
        """Write and verify one backup. Raises BackupError (and keeps nothing) when it cannot."""
        if kind not in KINDS:
            raise ValueError(f"unknown backup kind {kind}")
        now = self.wall()
        meta = {"kind": kind, "created_ts": now, **(info or {})}
        data = archive.build(doc, market, sessions, meta, raw=raw)
        with self.lock:
            try:
                os.makedirs(self.dir, exist_ok=True)
                base = f"paperdesk-{kind}-{fsutil.stamp(now)}"
                name, n = f"{base}.zip", 1
                while os.path.exists(os.path.join(self.dir, name)):
                    n += 1
                    name = f"{base}-{n}.zip"
                path = os.path.join(self.dir, name)
                fsutil.atomic_write_bytes(path, data)
            except OSError as e:
                self.last_error = f"could not write a backup: {e.strerror or e}"
                raise BackupError(self.last_error) from None
            try:
                archive.read(fsutil.read_bytes(path), max_upload=max(archive.MAX_UPLOAD, len(data) + 1))
            except (archive.ArchiveError, OSError) as e:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                self.last_error = f"a backup failed its check after writing and was deleted ({e})"
                raise BackupError(self.last_error) from None
            self.last_error = None
            if kind == "auto":
                self.last_auto = now
            self.prune()
        row = self.parse(name)
        row["size"] = len(data)
        return row

    def due(self) -> bool:
        if self.interval <= 0:
            return False
        last = self.last_auto
        if last is None:
            autos = [r for r in self.list() if r["kind"] == "auto"]
            last = self.last_auto = autos[0]["created_ts"] if autos else float("-inf")
        return self.wall() - last >= self.interval

    # ---- pruning ------------------------------------------------------------------------------------
    def plan_prune(self, rows: Optional[list] = None) -> list:
        """Names that pruning would delete (newest first in `rows`)."""
        rows = self.list() if rows is None else rows
        keep = set()
        autos = [r for r in rows if r["kind"] == "auto"]
        hours, days = [], []
        for r in autos:  # newest first: the first one seen in a bucket is that bucket's newest
            h = int(r["created_ts"] // 3600)
            d = datetime.fromtimestamp(r["created_ts"], timezone.utc).date()
            if h not in hours and len(hours) < self.keep_hourly:
                hours.append(h)
                keep.add(r["name"])
            if d not in days and len(days) < self.keep_daily:
                days.append(d)
                keep.add(r["name"])
        if autos:
            keep.add(autos[0]["name"])  # never the newest
        events = [r for r in rows if r["kind"] in EVENT_KINDS]
        keep.update(r["name"] for r in events[:self.keep_events])
        keep.update(r["name"] for r in rows if r["kind"] == "manual")
        return [r["name"] for r in rows if r["name"] not in keep]

    def prune(self) -> list:
        gone = []
        for name in self.plan_prune():
            try:
                os.unlink(os.path.join(self.dir, name))
                gone.append(name)
            except OSError:
                pass
        return gone

    def delete(self, name: str) -> None:
        os.unlink(self.path_of(name))

    # ---- reading ------------------------------------------------------------------------------------
    def read(self, name: str) -> archive.Archive:
        data = fsutil.read_bytes(self.path_of(name))
        return archive.read(data, max_upload=max(archive.MAX_UPLOAD, len(data) + 1))

    def latest_good(self) -> Optional[tuple]:
        """(row, Archive) of the newest backup that passes every check, or None."""
        for r in self.list():
            try:
                return r, self.read(r["name"])
            except (archive.ArchiveError, OSError, KeyError):
                continue
        return None
