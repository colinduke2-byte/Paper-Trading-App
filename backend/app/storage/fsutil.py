"""Small file-system helpers that behave the same on Windows and POSIX."""
import os
import tempfile
import time
from datetime import datetime, timezone


def fsync_dir(path: str) -> None:
    """Make a rename or a new file in `path` durable. POSIX only: Windows cannot open a directory for
    fsync, and NTFS journals the directory entry itself."""
    if os.name != "posix":
        return
    try:
        fd = os.open(path or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def replace(src: str, dst: str) -> None:
    """os.replace with a short retry: on Windows it raises PermissionError while another process (a virus
    scanner, an editor, a backup tool) briefly holds the target open."""
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Write `data` to `path` so that a crash leaves either the old file or the new one, never a mix.

    The temporary file is created in the target directory, so the rename never crosses volumes.
    """
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(d)


def read_bytes(path: str) -> bytes:
    """The whole file, opened, read and closed in one step.

    On Windows, opening a file raises PermissionError while it is being replaced (our own save thread) or
    held open by another program (a virus scanner, a backup tool), so retry briefly there; elsewhere
    PermissionError is a real permission problem and is raised at once.
    """
    attempts = 10 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            with open(path, "rb") as f:
                return f.read()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02 * (attempt + 1))
    raise AssertionError("unreachable")


def stamp(ts: float) -> str:
    """A UTC time stamp that is safe in file names on every platform: 20261124T093000Z."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
