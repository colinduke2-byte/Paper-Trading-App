"""Incremental indicators for indicator alerts (chart data: floats are allowed here).

Each calculator takes one closed bar's close at a time (`push`) and keeps O(period) state, so an
alert never re-reads the whole bar history on a tick. `value` is None until enough bars arrived.

Conventions (the usual charting definitions):
- SMA(n): mean of the last n closes.
- EMA(n): alpha = 2 / (n + 1), seeded with the SMA of the first n closes.
- RSI(n): Wilder's RSI. The first average gain and loss are the simple means of the first n
  changes; after that avg = (avg * (n - 1) + x) / n. RSI is 100 when there were no losses.
- MACD(12, 26, 9): EMA(12) - EMA(26); signal = EMA(9) of the MACD line, started once the MACD
  line exists. `value` is (macd, signal) once the signal exists.
"""
from collections import deque
from typing import Optional


class SMA:
    def __init__(self, n: int):
        self.n, self.window, self.total = n, deque(), 0.0

    def push(self, x: float) -> None:
        self.window.append(x)
        self.total += x
        if len(self.window) > self.n:
            self.total -= self.window.popleft()

    @property
    def value(self) -> Optional[float]:
        return self.total / self.n if len(self.window) == self.n else None


class EMA:
    def __init__(self, n: int):
        self.n, self.alpha, self.seed, self._v = n, 2.0 / (n + 1), [], None

    def push(self, x: float) -> None:
        if self._v is None:
            self.seed.append(x)
            if len(self.seed) == self.n:
                self._v, self.seed = sum(self.seed) / self.n, []
        else:
            self._v += self.alpha * (x - self._v)

    @property
    def value(self) -> Optional[float]:
        return self._v


class RSI:
    def __init__(self, n: int = 14):
        self.n, self.prev, self.gains, self.losses = n, None, [], []
        self.avg_gain = self.avg_loss = None

    def push(self, x: float) -> None:
        if self.prev is None:
            self.prev = x
            return
        ch, self.prev = x - self.prev, x
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if self.avg_gain is None:
            self.gains.append(g)
            self.losses.append(l)
            if len(self.gains) == self.n:
                self.avg_gain, self.avg_loss = sum(self.gains) / self.n, sum(self.losses) / self.n
                self.gains, self.losses = [], []
        else:
            self.avg_gain = (self.avg_gain * (self.n - 1) + g) / self.n
            self.avg_loss = (self.avg_loss * (self.n - 1) + l) / self.n

    @property
    def value(self) -> Optional[float]:
        if self.avg_gain is None:
            return None
        if self.avg_loss == 0:
            return 100.0 if self.avg_gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + self.avg_gain / self.avg_loss)


class MACD:
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast, self.slow, self.signal = EMA(fast), EMA(slow), EMA(signal)

    def push(self, x: float) -> None:
        self.fast.push(x)
        self.slow.push(x)
        if self.fast.value is not None and self.slow.value is not None:
            self.signal.push(self.fast.value - self.slow.value)

    @property
    def value(self) -> Optional[tuple]:
        if self.signal.value is None:
            return None
        return (self.fast.value - self.slow.value, self.signal.value)


def make(indicator: str, period: int):
    if indicator == "sma":
        return SMA(period)
    if indicator == "ema":
        return EMA(period)
    if indicator == "rsi":
        return RSI(period)
    if indicator == "macd":
        return MACD()
    raise ValueError(f"unknown indicator {indicator}")


def warmup(indicator: str, period: int) -> int:
    """Closed bars needed before the indicator has a value (MACD: 26 + 9 - 1)."""
    return 34 if indicator == "macd" else period + (1 if indicator == "rsi" else 0)


# ---- batch reference implementations (tests compare the incremental ones against these) ----------
def sma_batch(xs: list, n: int) -> Optional[float]:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema_batch(xs: list, n: int) -> Optional[float]:
    if len(xs) < n:
        return None
    v, a = sum(xs[:n]) / n, 2.0 / (n + 1)
    for x in xs[n:]:
        v = a * x + (1 - a) * v
    return v


def rsi_batch(xs: list, n: int) -> Optional[float]:
    ch = [b - a for a, b in zip(xs, xs[1:])]
    if len(ch) < n:
        return None
    ag = sum(max(c, 0.0) for c in ch[:n]) / n
    al = sum(max(-c, 0.0) for c in ch[:n]) / n
    for c in ch[n:]:
        ag = (ag * (n - 1) + max(c, 0.0)) / n
        al = (al * (n - 1) + max(-c, 0.0)) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + ag / al)
