"""One server per data directory.

The lock is an operating-system lock on <data>/.paperdesk.lock (fcntl.flock on Linux and macOS, msvcrt.locking
on Windows), held for as long as the process runs. The operating system drops it when the process ends, however
it ends, so a lock file left behind by a crash or a kill never blocks the next start: "stale" simply means the
file exists but nobody holds the lock on it. The holder writes who it is (process id, host, start time) to
<data>/.paperdesk.lock.json, which is only used for the message a second process prints.

Where the file system cannot lock files at all (some network shares), the fallback rule is the info file: it
counts as held while the same host's process id is still running (POSIX), or while the holder refreshes it.
The holder refreshes it from its own thread every HEARTBEAT_SECONDS; older than STALE_SECONDS means stale.
Two limits of that fallback, by nature: the age rule decides before the pid check, so a holder that stops
refreshing for STALE_SECONDS (a suspended laptop) loses the folder; and the check-then-write is not atomic, so
two servers started within the same moment on such a file system could both pass. The OS lock has neither.

Only the live exchange holds the lock: a replay swaps in a sandbox exchange with its own folder inside the data
folder, and the live exchange (ctx.live), which carries data_lock, never changes.
"""
import errno
import json
import os
import socket
import time
from typing import Optional

LOCK, INFO = ".paperdesk.lock", ".paperdesk.lock.json"
STALE_SECONDS = 600.0
HEARTBEAT_SECONDS = 60.0


class DataDirLocked(RuntimeError):
    """Another PaperDesk process is using this data directory."""


def _windows_pid_running(pid: int) -> bool:
    """OpenProcess + GetExitCodeProcess. No such process -> False; one we may not open (another user's) -> True;
    anything unexpected -> True, so the heartbeat age decides as before."""
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists; 87 (invalid parameter): it does not
        try:
            code = wintypes.DWORD()
            if k.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001 - never let the check itself stop a start-up
        return True


def _pid_running(pid: int) -> bool:
    if os.name == "nt":
        return _windows_pid_running(pid)
    if os.name != "posix":
        return True  # no cheap check here: rely on the heartbeat age
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


class DataDirLock:
    def __init__(self, data_dir: str, wall=time.time):
        self.dir = data_dir
        self.wall = wall
        self.path = os.path.join(data_dir, LOCK)
        self.info_path = os.path.join(data_dir, INFO)
        self._fh = None
        self.fallback = False

    def _info(self) -> Optional[dict]:
        try:
            with open(self.info_path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    def _message(self, info: Optional[dict]) -> str:
        who = ""
        if info:
            started = info.get("started")
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(started)) if isinstance(started, (int, float)) else "?"
            who = f" (process {info.get('pid', '?')} on {info.get('host', '?')}, started {when})"
        return (f"Another PaperDesk server{who} is already using the data folder {os.path.abspath(self.dir)}. "
                "Two servers writing the same files would overwrite each other's trades. Stop the other one first, "
                "or start this one with a different PAPERDESK_DATA folder.")

    def acquire(self) -> "DataDirLock":
        os.makedirs(self.dir, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            locked = self._os_lock(fh)
        except OSError as e:
            if e.errno in (errno.ENOLCK, errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", -1), errno.EINVAL):
                locked = None  # this file system cannot lock: use the info-file rule
            else:
                fh.close()
                raise
        if locked is False:
            fh.close()
            raise DataDirLocked(self._message(self._info()))
        if locked is None:
            self.fallback = True
            info = self._info()
            if info and not self._stale(info):
                fh.close()
                raise DataDirLocked(self._message(info))
        self._fh = fh
        self._write_info()
        if self.fallback:
            import threading
            self._stop = threading.Event()
            threading.Thread(target=self._beat, args=(self._stop,), daemon=True, name="paperdesk-lock").start()
        return self

    def _beat(self, stop) -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            self.heartbeat()

    def _os_lock(self, fh) -> Optional[bool]:
        """True when this process now holds the lock, False when someone else does."""
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                if e.errno in (errno.EACCES, errno.EDEADLK, 36):
                    return False
                raise
            return True
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EACCES):
                return False
            raise
        return True

    def _stale(self, info: dict) -> bool:
        beat = info.get("heartbeat", info.get("started"))
        if not isinstance(beat, (int, float)) or self.wall() - beat > STALE_SECONDS:
            return True
        if info.get("host") == socket.gethostname() and isinstance(info.get("pid"), int):
            return not _pid_running(info["pid"])
        return False

    def _write_info(self) -> None:
        now = self.wall()
        data = {"pid": os.getpid(), "host": socket.gethostname(), "started": now, "heartbeat": now}
        old = self._info() if self._fh is not None else None
        if old and old.get("pid") == os.getpid():
            data["started"] = old.get("started", now)
        try:
            tmp = self.info_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.info_path)
        except OSError:
            pass  # only used for messages

    def heartbeat(self) -> None:
        if self._fh is not None:
            self._write_info()

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        stop = getattr(self, "_stop", None)
        if stop is not None:
            stop.set()
        try:
            os.unlink(self.info_path)
        except OSError:
            pass
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    @property
    def held(self) -> bool:
        return self._fh is not None
