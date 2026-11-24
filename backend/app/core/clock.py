"""One clock for the whole program: real in production, simulated in tests."""
import time


class Clock:
    def now(self) -> float:
        return time.time()


class SimClock(Clock):
    def __init__(self, start: float = 0.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def set(self, t: float) -> None:
        self._t = float(t)

    def advance(self, seconds: float) -> float:
        self._t += seconds
        return self._t


class OffsetClock(Clock):
    """Real time shifted so that it starts at `start` (for tests and demos: PAPERDESK_CLOCK_START)."""

    def __init__(self, start: float, wall=time.time):
        self._wall = wall
        self._offset = float(start) - wall()

    def now(self) -> float:
        return self._wall() + self._offset


def parse_start(text: str) -> float:
    """'2026-11-28T15:00:00Z' (or with an offset) -> UNIX time. Raises ValueError naming the expected form."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"PAPERDESK_CLOCK_START must look like 2026-11-28T15:00:00Z, not {text!r}")
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
