"""Per-feed data for discovery: one row of fields per symbol, within a provider's rate limit.

Local feeds (synthetic, replay, scripted) have every quote and all history in memory, so every
symbol is screened live and nothing is budgeted.

A remote feed (Alpaca, ~11,000 symbols, 200 REST calls a minute on the free plan, shared with
the quote poller and charts) is handled differently:
- Quotes for the whole universe come from a background-free "scan": each discovery request may
  fetch a few batches of 100 snapshots, never more than SCAN_PER_MINUTE calls in any minute.
  A full pass takes about 11,000 / 100 / SCAN_PER_MINUTE minutes; each row says when it was fetched.
- Daily history (52-week range, moving averages, RSI, relative volume) costs one call per symbol,
  so it is fetched for at most HISTORY_PER_MINUTE symbols a minute and kept for HISTORY_TTL.
- Nothing here changes what the live stream subscribes to: scanned symbols are not added to the
  feed's interest, so the 30-symbol stream stays on orders, positions and what the browser shows.
"""
import threading
from collections import deque
from datetime import date, timedelta
from typing import Callable, Optional

from ..core import calendar as cal
from . import stats

SCAN_PER_MINUTE = 20
HISTORY_PER_MINUTE = 20
SCAN_BATCH = 100
SCAN_REFRESH = 900.0  # a scanned quote older than this is fetched again
HISTORY_TTL = 6 * 3600.0
INTRADAY_TTL = 300.0
FAILED_TTL = 300.0
INDEX_SYMBOLS_REMOTE = ["SPY", "QQQ", "IWM", "DIA"]
SECTOR_ETFS = {"XLK": "Technology", "XLV": "Health Care", "XLF": "Financials", "XLE": "Energy",
               "XLY": "Consumer Discretionary", "XLP": "Consumer Staples", "XLI": "Industrials",
               "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real Estate", "XLC": "Communication"}


class Budget:
    """At most `per_minute` calls in any rolling 60 seconds of `clock`."""

    def __init__(self, per_minute: int, clock: Callable[[], float]):
        self.per_minute, self.clock = per_minute, clock
        self.calls: deque = deque()
        self.lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self.calls and self.calls[0] <= now - 60:
            self.calls.popleft()

    def take(self) -> bool:
        with self.lock:
            now = self.clock()
            self._prune(now)
            if len(self.calls) >= self.per_minute:
                return False
            self.calls.append(now)
            return True

    def used(self) -> int:
        with self.lock:
            self._prune(self.clock())
            return len(self.calls)


def _f(v) -> Optional[float]:
    return None if v is None else float(v)


class FeedData:
    def __init__(self, feed, now: Callable[[], float]):
        self.feed = feed
        self.remote = getattr(feed, "rest", None) is not None
        self.clock = getattr(feed, "wall_clock", None) if self.remote else now
        self.scan_budget = Budget(SCAN_PER_MINUTE, self.clock)
        self.history_budget = Budget(HISTORY_PER_MINUTE, self.clock)
        self._hist: dict = {}   # symbol -> (fetched_at, daily bars or None)
        self._intra: dict = {}  # symbol -> (fetched_at, 5-minute bars or None)
        self._scan: dict = {}   # symbol -> (fetched_at, Quote)
        self._scan_cursor = 0
        self._scan_lock = threading.Lock()
        self.scan_error: Optional[str] = None
        self.provider_calls = 0  # calls made by this module (tests and the status line read it)

    # ---- dates ----------------------------------------------------------------------------------
    def today(self, ts: Optional[float] = None) -> date:
        ts = self.clock() if ts is None else ts
        if self.remote:
            return cal.ny_date(ts)  # Alpaca daily bars carry the New York trading date
        return date(1970, 1, 1) + timedelta(days=int(ts // 86400))  # local feeds roll the day at UTC midnight

    # ---- quotes ---------------------------------------------------------------------------------
    def quote(self, symbol: str):
        """(Quote or None, source label, fetched-at seconds or None)."""
        q = self.feed.quote(symbol)
        if not self.remote:
            return q, "simulated" if self.feed.simulated else self.feed.name, None
        hit = self._scan.get(symbol)
        if q is not None and (hit is None or q.ts >= hit[1].ts):
            return q, "Alpaca (live)", None
        if hit is not None:
            return hit[1], "Alpaca snapshot (universe scan)", hit[0]
        return None, "", None

    def universe(self) -> list:
        return list(self.feed.symbols())

    def scan(self, max_batches: int = 3) -> int:
        """Remote feeds: fetch up to `max_batches` snapshot batches within the scan budget. Returns batches fetched."""
        if not self.remote or not self._scan_lock.acquire(blocking=False):
            return 0
        try:
            from ..marketdata.alpaca import SymbolState
            from ..marketdata.http import ProviderError
            syms = sorted(self.feed.symbols())
            if not syms:
                return 0
            # Index and sector funds first, so the overview fills in before the long tail.
            first = [s for s in INDEX_SYMBOLS_REMOTE + list(SECTOR_ETFS) if s in self.feed.symbols()]
            done = 0
            for _ in range(max_batches):
                now = self.clock()
                stale = lambda s: s not in self._scan or now - self._scan[s][0] >= SCAN_REFRESH
                batch = [s for s in first if stale(s)]
                if len(batch) < SCAN_BATCH:
                    n, start = len(syms), self._scan_cursor
                    i = 0
                    while len(batch) < SCAN_BATCH and i < n:
                        s = syms[(start + i) % n]
                        if stale(s) and s not in batch:
                            batch.append(s)
                        i += 1
                    self._scan_cursor = (start + i) % n
                if not batch or not self.scan_budget.take():
                    break
                self.provider_calls += 1
                try:
                    snaps = self.feed.rest.snapshots(batch, self.feed.data_feed, max_wait=2)
                except ProviderError as e:
                    self.scan_error = f"The universe scan paused: {e.message}"
                    break
                self.scan_error = None
                today = cal.ny_date(now)
                for s in batch:
                    snap = (snaps or {}).get(s)
                    q = None
                    if snap:
                        st = SymbolState(s)
                        if st.apply_snapshot(snap, today):
                            q = st.to_quote()
                    # No data (halted, no trades yet): keep any older quote but note the attempt so the scan moves on.
                    self._scan[s] = (now, q if q is not None else (self._scan.get(s) or (0, None))[1])
                done += 1
            return done
        finally:
            self._scan_lock.release()

    def scanned(self) -> dict:
        return {s: v for s, v in list(self._scan.items()) if v[1] is not None}  # list(): other requests may be scanning

    # ---- history --------------------------------------------------------------------------------
    def daily(self, symbol: str, fetch: bool = True) -> Optional[list]:
        if not self.remote:
            return self.feed.bars(symbol, "1d", 300)
        return self._cached(self._hist, symbol, HISTORY_TTL, fetch, lambda: self.feed.bars(symbol, "1d", 300))

    def intraday(self, symbol: str, fetch: bool = True) -> Optional[list]:
        if not self.remote:
            return self.feed.bars(symbol, "5m", 78)
        return self._cached(self._intra, symbol, INTRADAY_TTL, fetch, lambda: self.feed.bars(symbol, "5m", 78))

    def has_history(self, symbol: str) -> bool:
        return not self.remote or bool((self._hist.get(symbol) or (0, None))[1])

    def _cached(self, cache: dict, symbol: str, ttl: float, fetch: bool, load) -> Optional[list]:
        now = self.clock()
        hit = cache.get(symbol)
        if hit and now - hit[0] < (ttl if hit[1] else FAILED_TTL):
            return hit[1]
        if not fetch or not self.history_budget.take():
            return hit[1] if hit else None
        self.provider_calls += 1
        rows = load()
        cache[symbol] = (now, rows or None)
        return rows or None

    # ---- rows -----------------------------------------------------------------------------------
    def info(self, symbol: str) -> dict:
        return self.feed.symbols().get(symbol) or {}

    def row(self, symbol: str, history: bool = True, fetch: bool = True) -> dict:
        """Every discovery field for one symbol. Missing data is None (never invented)."""
        info = self.info(symbol)
        q, source, fetched = self.quote(symbol)
        r = {"symbol": symbol, "name": info.get("name") or symbol, "type": info.get("type") or "stock",
             "sector": info.get("sector") or None, "exchange": info.get("exchange") or None,
             "quote_source": source if q is not None else None, "quote_fetched_at": fetched}
        last = _f(q.last) if q is not None else None
        prev = _f(q.prev_close) if q is not None and q.prev_close else None
        r.update(last=last, bid=_f(q.bid) if q else None, ask=_f(q.ask) if q else None,
                 open=_f(q.open) if q else None, high=_f(q.high) if q else None, low=_f(q.low) if q else None,
                 prev_close=prev, volume=_f(q.volume) if q else None, quote_ts=q.ts if q else None)
        r["change"] = last - prev if last is not None and prev else None
        r["change_pct"] = stats.pct(last, prev)
        r["gap_pct"] = stats.pct(r["open"], prev)
        r["dollar_volume"] = last * r["volume"] if last is not None and r["volume"] is not None else None
        # Fundamentals only from what the feed exposes (the synthetic companies carry them; Alpaca does not).
        shares, eps, div = info.get("shares_outstanding"), info.get("eps"), info.get("dividend")
        mc = info.get("market_cap")
        r["market_cap"] = float(mc) if mc is not None else (last * float(shares) if shares and last else None)
        pe = info.get("pe")
        r["pe"] = float(pe) if pe is not None else (last / float(eps) if eps and float(eps) > 0 and last else None)
        dy = info.get("dividend_yield_pct")
        r["dividend_yield"] = float(dy) if dy is not None else (float(div) / last * 100 if div is not None and last else None)
        r["has_options"] = True if not self.remote else info.get("has_options")
        if history:
            daily = self.daily(symbol, fetch=fetch)
            hist, today_bar = stats.split_today(daily or [], self.today(q.ts if q else None))
            if hist:
                r.update(stats.history_stats(hist, last, today_bar, r["volume"]))
            r["history"] = bool(hist)  # completed daily bars exist (today's own bar is not history)
        return r
