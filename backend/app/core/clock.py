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
