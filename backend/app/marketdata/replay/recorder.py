"""Record any MarketDataProvider into a session file."""
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Iterable, Optional

from ...core import calendar as cal
from ...core.money import ZERO
from ..feed import MarketDataProvider
from .format import SessionWriter


def _info_row(info: dict) -> dict:
    out = {}
    for k, v in info.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, Decimal):
            out[k] = float(v)
    return out


class SessionRecorder:
    """Writes what a provider produces: a header from its current state, then every change it reports.

    Call start(now) once, capture(quotes) with each list provider.step() returns, then finish().
    """

    def __init__(self, provider: MarketDataProvider, path, symbols: Optional[list] = None, label: str = "",
                 description: str = "", source: Optional[str] = None, simulated: Optional[bool] = None,
                 daily_days: int = 260, include_minutes: bool = True, fresh_day: bool = False):
        self.provider = provider
        self.path = path
        self.symbols = [s for s in (symbols or sorted(provider.symbols())) if s in provider.symbols()]
        if not self.symbols:
            raise ValueError("nothing to record: none of the requested symbols is in the feed")
        self._wanted = set(self.symbols)
        self.label, self.description = label, description
        self.source = source or provider.name
        self.simulated = provider.simulated if simulated is None else simulated
        self.daily_days, self.include_minutes, self.fresh_day = daily_days, include_minutes, fresh_day
        self.writer: Optional[SessionWriter] = None
        self._book: dict = {}
        self.end: Optional[float] = None

    def start(self, now: float, planned_end: Optional[float] = None) -> None:
        p = self.provider
        day = cal.ny_date(now)
        initial = {}
        for s in self.symbols:
            q = p.quote(s)
            if q is None:
                raise ValueError(f"the feed has no quote for {s} yet")
            row = {"bid": q.bid, "ask": q.ask, "bid_size": q.bid_size, "ask_size": q.ask_size, "last": q.last,
                   "open": q.open, "high": q.high, "low": q.low, "volume": q.volume, "prev_close": q.prev_close}
            if self.fresh_day:  # start the day clean: no volume yet, range is the current price
                row.update(open=q.last, high=q.last, low=q.last, volume=ZERO)
            initial[s] = {k: (None if v is None else str(v)) for k, v in row.items()}
            self._book[s] = (q.bid, q.ask, q.bid_size, q.ask_size)
        header = {"label": self.label or f"Session of {day.isoformat()}", "description": self.description,
                  "date": day.isoformat(), "start": now, "end": planned_end, "timezone": "America/New_York",
                  "source": self.source, "simulated": bool(self.simulated),
                  "symbols": [_info_row(p.symbols()[s]) for s in self.symbols], "initial": initial}
        self.writer = SessionWriter(self.path)
        self.writer.start(header)
        midnight = int((day - date(1970, 1, 1)).total_seconds())
        for s in self.symbols:
            rows = [r for r in p.store.daily.get(s, []) if r[0] < midnight][-self.daily_days:]
            if rows:
                self.writer.context("daily", s, rows)
        if self.include_minutes and not self.fresh_day:
            day_start = cal.ny_datetime(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            for s in self.symbols:
                rows = [r for r in p.store.minutes.get(s, ()) if day_start <= r[0] < now]
                if rows:
                    self.writer.context("minutes", s, rows)

    def capture(self, quotes: Iterable) -> int:
        n = 0
        w = self.writer
        for q in quotes:
            if q.symbol not in self._wanted:
                continue
            book = (q.bid, q.ask, q.bid_size, q.ask_size)
            if book != self._book.get(q.symbol):
                w.quote(q.ts, q.symbol, *book)
                self._book[q.symbol] = book
                n += 1
            if q.trade and q.last_size > 0:
                w.trade(q.ts, q.symbol, q.last, q.last_size)
                n += 1
        return n

    def finish(self, end: Optional[float] = None) -> dict:
        self.writer.close(end)
        self.end = end
        return {"path": str(self.path), "events": self.writer.events, "symbols": len(self.symbols)}


def record(provider: MarketDataProvider, path, times: Iterable[float], end: Optional[float] = None,
           on_step: Optional[Callable[[float], None]] = None, **kw) -> dict:
    """Drive `provider` through `times` (ascending) and record it. The first time is the session start."""
    it = iter(times)
    first = next(it)
    rec = SessionRecorder(provider, path, **kw)
    rec.start(first, end)
    rec.capture(provider.step(first))
    last = first
    for t in it:
        rec.capture(provider.step(t))
        last = t
        if on_step:
            on_step(t)
    return rec.finish(end if end is not None else last)


def realtime(seconds: float, interval: float = 1.0, sleep=time.sleep, clock=time.time) -> Iterable[float]:
    """Wall-clock times for recording a live provider as it runs (used by the CLI, not by the exchange)."""
    t0 = clock()
    k = 0
    while True:
        target = t0 + k * interval
        if target - t0 > seconds:
            return
        delay = target - clock()
        if delay > 0:
            sleep(delay)
        yield target
        k += 1


def session_times(day: date, step: float = 5.0, start: Optional[float] = None, end: Optional[float] = None) -> list:
    """Every `step` seconds through the regular session of `day` (early closes respected)."""
    if not cal.is_trading_day(day):
        raise ValueError(f"{day.isoformat()} is not a US trading day")
    open_ts = datetime.combine(day, cal.OPEN, tzinfo=cal.NY).timestamp()
    close = cal.close_ts(day)
    a, b = start or open_ts, end or close
    out, t = [], a
    while t <= b:
        out.append(t)
        t += step
    return out

