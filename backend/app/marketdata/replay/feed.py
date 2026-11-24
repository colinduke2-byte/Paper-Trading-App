"""ReplayFeed: plays a recorded session back, driven entirely by the time the exchange passes in."""
import zlib
from collections import deque
from decimal import Decimal
from typing import Optional

from ...core.models import Quote
from ...core.money import CENT, ZERO
from ..feed import MarketDataProvider
from .format import QUOTE, TRADE, SessionData, read_session


class ReplayFeed(MarketDataProvider):
    """Deterministic playback. step(now) releases every recorded event with a time at or before `now`.

    The feed has no clock of its own: the exchange's (simulated) clock decides how far it goes, so the
    same file and the same sequence of clock times always produce identical quotes. Time only moves
    forward; to go back, start the session again.
    """
    name = "replay"
    delay_seconds = 0

    def __init__(self, session: SessionData):
        super().__init__()
        self.session = session
        h = session.header
        self.simulated = bool(h.get("simulated", True))
        self.source = h.get("source", "recorded")
        self.start, self.end = session.start, session.end
        for s in h["symbols"]:
            info = dict(s)
            info.setdefault("name", info["symbol"])
            info.setdefault("sector", "Unknown")
            info.setdefault("vol", 0.3)
            self.info[info["symbol"]] = info
        for sym, rows in session.daily.items():
            self.store.daily[sym] = [list(r) for r in rows]
        for sym, rows in session.minutes.items():
            self.store.minutes[sym] = deque((list(r) for r in rows), maxlen=self.store.max_minutes)
        for sym in self.info:
            init = session.initial(sym)
            last = init.get("last") or init.get("prev_close") or init.get("bid") or CENT
            self.quotes[sym] = Quote(
                symbol=sym, bid=init.get("bid", last), ask=init.get("ask", last),
                bid_size=init.get("bid_size", ZERO), ask_size=init.get("ask_size", ZERO), last=last,
                volume=init.get("volume") or ZERO, ts=self.start, trade=False,
                open=init.get("open") or last, high=init.get("high") or last, low=init.get("low") or last,
                prev_close=init.get("prev_close") or last)
        self._events = session.events
        self._i = 0
        self.last_step: Optional[float] = None

    @classmethod
    def from_file(cls, path) -> "ReplayFeed":
        return cls(read_session(path))

    # ---- playback -------------------------------------------------------
    def step(self, now: float) -> list:
        if self.last_step is not None and now < self.last_step:
            raise ValueError("a replay cannot go back in time; start the session again")
        self.last_step = now
        ev, i, n = self._events, self._i, len(self._events)
        out = []
        while i < n and ev[i][0] <= now:
            e = ev[i]
            i += 1
            ts, kind, sym = e[0], e[1], e[2]
            prev = self.quotes[sym]
            bid, ask, bid_size, ask_size = prev.bid, prev.ask, prev.bid_size, prev.ask_size
            trade = None
            if kind == QUOTE:
                bid, ask, bid_size, ask_size = e[3], e[4], e[5], e[6]
                # A quote followed at the same moment by a print of the same symbol is one update.
                if i < n and ev[i][1] == TRADE and ev[i][0] == ts and ev[i][2] == sym:
                    trade = ev[i]
                    i += 1
            elif kind == TRADE:
                trade = e
            if trade is not None:
                price, size = trade[3], trade[4]
                q = Quote(symbol=sym, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size, last=price,
                          last_size=size, volume=prev.volume + size, ts=ts, trade=True, open=prev.open,
                          high=max(prev.high, price), low=min(prev.low, price), prev_close=prev.prev_close)
            else:
                q = Quote(symbol=sym, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size, last=prev.last,
                          last_size=ZERO, volume=prev.volume, ts=ts, trade=False, open=prev.open,
                          high=prev.high, low=prev.low, prev_close=prev.prev_close)
            self.quotes[sym] = q
            if q.trade:
                self._record_print(q)
            out.append(q)
        self._i = i
        return out

    @property
    def finished(self) -> bool:
        return self._i >= len(self._events)

    def progress(self, now: float) -> dict:
        span = max(self.end - self.start, 1e-9)
        return {"start": self.start, "end": self.end, "fraction": min(max((now - self.start) / span, 0.0), 1.0),
                "events_played": self._i, "events_total": len(self._events)}

    def depth(self, symbol: str, levels: int = 10) -> dict:
        """Synthesised book like the base feed, but with a stable hash so playback stays reproducible."""
        q = self.quotes.get(symbol)
        if q is None:
            return {"simulated": True, "bids": [], "asks": []}
        tick = CENT if q.last < 50 else CENT * (1 + int(q.last / 100))
        bids, asks = [], []
        for i in range(levels):
            wobble = zlib.crc32(f"{symbol}:{i}:{int(q.ts) // 5}".encode()) % 7
            scale = Decimal(1) + Decimal(i) * Decimal("0.6") + Decimal(wobble) / 10
            bids.append({"price": q.bid - tick * i, "size": (q.bid_size * scale).quantize(Decimal(1))})
            asks.append({"price": q.ask + tick * i, "size": (q.ask_size * scale).quantize(Decimal(1))})
        return {"simulated": True, "bids": [b for b in bids if b["price"] > 0], "asks": asks}
