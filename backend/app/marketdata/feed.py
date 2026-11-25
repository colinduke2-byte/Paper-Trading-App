"""Market data provider interface, bar storage and the scripted test feed."""
from collections import deque
from dataclasses import replace
from decimal import Decimal
from typing import Optional

from ..core.assets import round_to, spec_from_info
from ..core.models import Quote
from ..core.money import CENT, ZERO, D, cents

TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400, "1w": 604800}


class BarStore:
    """One-minute bars built from prints, plus daily bars. Prices kept as floats for charting."""

    def __init__(self, max_minutes: int = 6000):
        self.minutes: dict[str, deque] = {}
        self.daily: dict[str, list] = {}
        self.max_minutes = max_minutes

    def add_print(self, symbol: str, ts: float, price: float, size: float) -> None:
        bars = self.minutes.setdefault(symbol, deque(maxlen=self.max_minutes))
        t = int(ts // 60) * 60
        if bars and bars[-1][0] == t:
            b = bars[-1]
            b[2], b[3], b[4], b[5] = max(b[2], price), min(b[3], price), price, b[5] + size
        else:
            bars.append([t, price, price, price, price, size])

    def bars(self, symbol: str, tf: str, limit: int = 500, today: Optional[list] = None) -> list:
        if tf not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe {tf}")
        if tf in ("1d", "1w"):
            rows = list(self.daily.get(symbol, []))
            if today:
                rows.append(today)
            if tf == "1w":
                rows = _aggregate(rows, 604800, offset=4 * 86400)  # weeks start on Monday
        else:
            rows = list(self.minutes.get(symbol, ()))
            if tf != "1m":
                rows = _aggregate(rows, TIMEFRAMES[tf])
        return [{"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
                for r in rows[-limit:]]


def _aggregate(rows: list, seconds: int, offset: int = 0) -> list:
    out = []
    for r in rows:
        t = (r[0] - offset) // seconds * seconds + offset
        if out and out[-1][0] == t:
            b = out[-1]
            b[2], b[3], b[4], b[5] = max(b[2], r[2]), min(b[3], r[3]), r[4], b[5] + r[5]
        else:
            out.append([t, r[1], r[2], r[3], r[4], r[5]])
    return out


class MarketDataProvider:
    """Interface every feed implements. Nothing else knows which feed is running."""
    name = "base"
    simulated = True
    delay_seconds = 0
    fallback_reason: Optional[str] = None  # why a real feed was not used, shown in /api/status
    research = None  # optional app.marketdata.research.ResearchHub

    def __init__(self):
        self.store = BarStore()
        self.quotes: dict[str, Quote] = {}
        self.info: dict[str, dict] = {}
        self.prints: dict[str, deque] = {}
        self.applied_actions: set = set()  # ids of corporate actions already reflected in prices

    def symbols(self) -> dict:
        return self.info

    def quote(self, symbol: str) -> Optional[Quote]:
        return self.quotes.get(symbol)

    def base_vol(self, symbol: str) -> float:
        return float(self.info.get(symbol, {}).get("vol", 0.3))

    def step(self, now: float) -> list:
        """Advance the feed to `now` and return the quotes that changed."""
        raise NotImplementedError

    def bars(self, symbol: str, tf: str, limit: int = 500) -> list:
        q = self.quotes.get(symbol)
        today = None
        if q is not None and q.open is not None:
            day = int(q.ts // 86400) * 86400
            today = [day, float(q.open), float(q.high), float(q.low), float(q.last), float(q.volume)]
        return self.store.bars(symbol, tf, limit, today)

    def trades(self, symbol: str, limit: int = 50) -> list:
        return list(self.prints.get(symbol, ()))[-limit:][::-1]

    def depth(self, symbol: str, levels: int = 10) -> dict:
        """Synthesised order book around the top of book. Not real depth."""
        q = self.quotes.get(symbol)
        if q is None:
            return {"simulated": True, "bids": [], "asks": []}
        tick = CENT if q.last < 50 else CENT * (1 + int(q.last / 100))
        size_step = Decimal(1)
        info = self.info.get(symbol) or {}
        if info.get("type") == "crypto":  # finer prices and fractional sizes
            spec = spec_from_info(symbol, info)
            tick = max(spec.price_increment, round_to(q.last * Decimal("0.0002"), spec.price_increment))
            size_step = spec.qty_increment
        sz = (lambda v: v.quantize(Decimal(1))) if size_step == 1 else (lambda v: round_to(v, size_step))
        bids, asks = [], []
        for i in range(levels):
            scale = Decimal(1) + Decimal(i) * Decimal("0.6") + Decimal((hash((symbol, i, int(q.ts) // 5)) % 7)) / 10
            bids.append({"price": q.bid - tick * i, "size": sz(q.bid_size * scale)})
            asks.append({"price": q.ask + tick * i, "size": sz(q.ask_size * scale)})
        return {"simulated": True, "bids": [b for b in bids if b["price"] > 0], "asks": asks}

    def _record_print(self, q: Quote) -> None:
        self.store.add_print(q.symbol, q.ts, float(q.last), float(q.last_size))
        self.prints.setdefault(q.symbol, deque(maxlen=200)).append(
            {"ts": q.ts, "price": q.last, "size": q.last_size,
             "side": "buy" if q.last >= q.ask else "sell" if q.last <= q.bid else "mid"})

    def snapshot(self) -> dict:
        return {}

    def restore(self, data: dict) -> None:
        pass

    # ---- optional provider data (a feed without it returns None) ----------
    def option_quote(self, symbol: str) -> Optional[Quote]:
        """A provider quote for an option contract, or None to use the model price."""
        return None

    def option_expirations(self, underlying: str) -> Optional[list]:
        """Listed expiry dates from the provider, or None to use the model calendar."""
        return None

    def option_chain(self, underlying: str, expiry, now: float) -> Optional[dict]:
        """Provider chain in the shape of options.chain.chain(), or None to use the model."""
        return None

    def event_variance(self, symbol: str, expiry_ts: float, now: float) -> float:
        """Extra variance (log return squared) the option model adds for scheduled events, such as an
        earnings report, between now and expiry. The research hub supplies it; default none."""
        f = getattr(self.research, "event_variance", None)
        return f(symbol, expiry_ts, now) if f is not None else 0.0

    def status_info(self) -> dict:
        """Extra fields for /api/status: data source, real-time or delayed, stream health."""
        if self.simulated:
            return {"realtime": False, "data_source": "simulated", "data_exchange": None,
                    "data_note": "Simulated prices for fictional companies; not market data.",
                    "fallback_reason": self.fallback_reason}
        return {"fallback_reason": self.fallback_reason}

    def close(self) -> None:
        """Stop background work. Feeds without any do nothing."""
    # ---- corporate actions (app.lifecycle.actions.CorporateAction records) -----------
    def corporate_actions(self, start, end) -> list:
        """Scheduled actions with an ex-date between start and end (dates, inclusive). Default: none."""
        return []

    def apply_corporate_action(self, action) -> None:
        """Called once when an action takes effect, so the feed can adjust its prices and history."""
        if action.id in self.applied_actions:
            return
        self.applied_actions.add(action.id)
        self._adjust_for_action(action)

    def _adjust_for_action(self, action) -> None:
        pass


def adjust_for_split(feed: MarketDataProvider, action) -> None:
    """Split-adjust a feed's current quote and stored bars (prices x old/new, volume x new/old)."""
    sym, n, o = action.symbol, Decimal(action.new), Decimal(action.old)
    px = lambda v: max(cents(v * o / n), CENT) if v is not None else None
    q = feed.quotes.get(sym)
    if q is not None:
        bid, ask = px(q.bid), px(q.ask)
        feed.quotes[sym] = replace(q, bid=bid, ask=max(ask, bid + CENT), last=px(q.last), open=px(q.open),
                                   high=px(q.high), low=px(q.low), prev_close=px(q.prev_close), volume=q.volume * n / o)
    f, vf = float(o / n), float(n / o)
    for rows in (feed.store.daily.get(sym, []), feed.store.minutes.get(sym, [])):
        for r in rows:
            r[1], r[2], r[3], r[4] = (round(v * f, 4) for v in r[1:5])
            r[5] = r[5] * vf
    for p in feed.prints.get(sym, ()):
        p["price"] = px(p["price"])


class SimFeed(MarketDataProvider):
    """Scripted feed for tests: quotes appear only when the test sets them."""
    name = "scripted"

    def __init__(self, symbols: Optional[dict] = None):
        super().__init__()
        self.info = symbols or {}
        self._pending: list[Quote] = []
        self.actions: list = []

    def schedule(self, action) -> None:
        """Announce a corporate action (tests use this)."""
        self.actions.append(action)

    def corporate_actions(self, start, end) -> list:
        return [a for a in self.actions if start <= a.ex_date <= end]

    def _adjust_for_action(self, action) -> None:
        if action.kind == "split":
            adjust_for_split(self, action)

    def set_quote(self, symbol: str, bid, ask, last=None, bid_size=1000, ask_size=1000, last_size=100,
                  trade: bool = True, ts: float = 0.0) -> Quote:
        bid, ask = D(bid), D(ask)
        last = D(last) if last is not None else ask
        prev = self.quotes.get(symbol)
        q = Quote(symbol=symbol, bid=bid, ask=ask, bid_size=D(bid_size), ask_size=D(ask_size), last=last,
                  last_size=D(last_size) if trade else ZERO, ts=ts, trade=trade,
                  volume=(prev.volume if prev else ZERO) + (D(last_size) if trade else ZERO),
                  open=prev.open if prev else last, high=max(prev.high, last) if prev else last,
                  low=min(prev.low, last) if prev else last, prev_close=prev.prev_close if prev else last)
        self.info.setdefault(symbol, {"symbol": symbol, "name": symbol, "sector": "Test", "vol": 0.3})
        self.quotes[symbol] = q
        self._pending.append(q)
        return q

    def step(self, now: float) -> list:
        out, self._pending = self._pending, []
        for q in out:
            q.ts = now
            if q.trade:
                self._record_print(q)
        return out
