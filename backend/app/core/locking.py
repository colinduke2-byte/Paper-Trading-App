"""The exchange lock: one re-entrant mutex per running server (see docs/CONCURRENCY.md).

Everything that reads or changes exchange state (accounts, ledger, orders, lifecycle, the feed's step,
mode switches, taking a snapshot for saving) runs while holding it. It differs from threading.RLock in
three ways the server needs:

- every wait can be bounded, so a request answers "busy" instead of queueing for ever;
- the tick asks with `urgent=True` and goes ahead of ordinary waiters, so requests cannot starve the market;
- ordinary waiters are served first come, first served, so no request loses the race again and again;
- it knows who holds it and for how long, so a stuck holder can be named in the log.

Only threading primitives are used, so it behaves the same on Windows, macOS and Linux.
"""
import contextlib
import threading
from collections import deque
import time
from typing import Optional

_local = threading.local()


class Busy(Exception):
    """The lock could not be taken in time. The API answers 503 with this message."""

    def __init__(self, message: str = "The simulator is busy. Try again in a moment."):
        super().__init__(message)


def held() -> bool:
    """True when the calling thread holds any StateLock. Used to refuse blocking work under the lock."""
    return getattr(_local, "depth", 0) > 0


class StateLock:
    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._owner: Optional[int] = None
        self._depth = 0
        self._urgent = 0  # urgent waiters; ordinary waiters let them go first
        self._queue = deque()  # ordinary waiters, oldest first
        self.label: Optional[str] = None  # who holds it now (diagnostics only)
        self.since = 0.0  # time.monotonic() when the current holder got it
        self.longest = (0.0, None)  # (seconds, label) of the longest hold so far

    # ---- core ----------------------------------------------------------------------------------
    def acquire(self, timeout: Optional[float] = None, urgent: bool = False, label: Optional[str] = None) -> bool:
        """Take the lock. `timeout` None waits for ever, 0 does not wait. Returns False on timeout."""
        me = threading.get_ident()
        with self._cond:
            if self._owner == me:
                self._depth += 1
                return True
            ticket = None
            if urgent:
                self._urgent += 1
                free = lambda: self._owner is None
            else:
                ticket = object()
                self._queue.append(ticket)
                free = lambda: self._owner is None and self._urgent == 0 and self._queue[0] is ticket
            try:
                ok = free() if timeout == 0 else self._cond.wait_for(free, timeout)
            finally:
                if urgent:
                    self._urgent -= 1
                else:
                    self._queue.remove(ticket)
                self._cond.notify_all()  # whoever is next may go
            if not ok:
                return False
            self._owner, self._depth = me, 1
            self.label, self.since = label, time.monotonic()
        _local.depth = getattr(_local, "depth", 0) + 1
        return True

    def release(self) -> None:
        with self._cond:
            if self._owner != threading.get_ident():
                raise RuntimeError("the exchange lock is released by a thread that does not hold it")
            self._depth -= 1
            if self._depth:
                return
            took = time.monotonic() - self.since
            if took > self.longest[0]:
                self.longest = (took, self.label)
            self._owner, self.label = None, None
            self._cond.notify_all()
        _local.depth -= 1

    def owned(self) -> bool:
        """True when the calling thread holds this lock."""
        return self._owner == threading.get_ident()

    def holder(self) -> Optional[tuple]:
        """(label, seconds held) of the current holder, or None when the lock is free."""
        with self._cond:
            return None if self._owner is None else (self.label, time.monotonic() - self.since)

    # ---- context managers ------------------------------------------------------------------------
    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()

    @contextlib.contextmanager
    def hold(self, timeout: Optional[float] = None, urgent: bool = False, label: Optional[str] = None):
        """`with lock.hold(2.0):` takes the lock or raises Busy after `timeout` seconds."""
        if not self.acquire(timeout, urgent, label):
            raise Busy()
        try:
            yield self
        finally:
            self.release()
