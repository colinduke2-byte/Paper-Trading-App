"""Alpaca crypto market data: USD pairs, latest quotes and trades, bars, order books and streaming.

Verified facts (docs.alpaca.markets, read 2026-11-24; see docs/CRYPTO.md for the page list):
- REST on data.alpaca.markets under /v1beta3/crypto/{loc}/ with loc "us" (Alpaca US): latest/quotes,
  latest/trades, latest/bars, latest/orderbooks, snapshots, bars (timeframe, start, end, limit up to
  10000 in total across symbols, page_token, sort asc or desc). Symbols are written BTC/USD.
- Quote sizes are in the base asset with no lot multiplier (unlike stock quotes).
- Stream: wss://stream.data.alpaca.markets/v1beta3/crypto/{loc}; same greeting, auth and error
  frames as the stock stream; channels trades, quotes, bars, orderbooks, updatedBars, dailyBars.
- Assets: GET /v2/assets?asset_class=crypto on the trading host, with min_order_size,
  min_trade_increment and price_increment per pair (strings).
- The OpenAPI pages list API-key authentication for every crypto endpoint, so the same key headers
  are sent. Crypto rate limits are not stated; requests share the stock token bucket.

Nothing here was run against the live service (no key was available); the tests use the documented
example responses saved in tests/fixtures/alpaca_crypto. Like the stock side, all network I/O runs on
the poller and stream threads; step(now) only drains a lock-protected map.

This module only reads market data. It has no code path that sends an order.
"""
import json
import logging
import time
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..core.assets import crypto_info, floor_to, is_crypto, spec_from_info
from ..core.money import ZERO
from .alpaca import (TF_ALPACA, TF_LOOKBACK_DAYS, TF_TTL, AlpacaFeed, StreamClient, StreamProtocol, SymbolState,
                     WebsocketsConnection, parse_bars)
from .feed import TIMEFRAMES
from .http import ProviderError, dec, parse_ts

log = logging.getLogger("paperdesk.alpaca.crypto")

CRYPTO_STREAM_URL = "wss://stream.data.alpaca.markets/v1beta3/crypto/{loc}"
CRYPTO_LOCATIONS = ("us", "us-1", "eu-1")
CRYPTO_NOTE = ("Crypto prices from Alpaca's crypto market data (location {loc}). Crypto trades 24 hours a day, "
               "every day; prices come from the venue Alpaca names for that location, not a consolidated tape.")


# ---------------------------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------------------------
class CryptoREST:
    """Crypto endpoints on top of an AlpacaREST client (same keys, same rate limiter)."""

    def __init__(self, rest, loc: str = "us"):
        self.rest, self.loc = rest, loc

    def _get(self, path: str, params: dict, **kw):
        return self.rest._data(f"/v1beta3/crypto/{self.loc}/{path}", params, **kw) or {}

    def assets(self, **kw) -> list:
        return self.rest.http.get(self.rest.trading_url, "/v2/assets", {"status": "active", "asset_class": "crypto"},
                                  max_wait=30, **kw) or []

    def snapshots(self, symbols: Iterable[str], **kw) -> dict:
        return self._get("snapshots", {"symbols": ",".join(symbols)}, **kw).get("snapshots") or {}

    def latest_quotes(self, symbols: Iterable[str], **kw) -> dict:
        return self._get("latest/quotes", {"symbols": ",".join(symbols)}, **kw).get("quotes") or {}

    def latest_trades(self, symbols: Iterable[str], **kw) -> dict:
        return self._get("latest/trades", {"symbols": ",".join(symbols)}, **kw).get("trades") or {}

    def latest_orderbooks(self, symbols: Iterable[str], **kw) -> dict:
        return self._get("latest/orderbooks", {"symbols": ",".join(symbols)}, **kw).get("orderbooks") or {}

    def bars(self, symbol: str, timeframe: str, start: str, end: Optional[str], limit: int, **kw) -> list:
        j = self._get("bars", {"symbols": symbol, "timeframe": timeframe, "start": start, "end": end,
                               "limit": limit, "sort": "desc"}, **kw)
        return (j.get("bars") or {}).get(symbol, [])


# ---------------------------------------------------------------------------------------------
# Parsing (pure, tested against the documentation examples)
# ---------------------------------------------------------------------------------------------
def assets_to_info(rows: list) -> dict:
    """Active, tradable USD pairs -> feed symbol records with the provider's increments."""
    out = {}
    for a in rows or []:
        sym = str(a.get("symbol") or "")
        if a.get("class") != "crypto" or not is_crypto(sym) or a.get("status", "active") != "active":
            continue
        if not a.get("tradable", False):
            continue
        out[sym] = crypto_info(sym, a.get("name") or sym, a.get("min_trade_increment") or "0.000000001",
                               a.get("price_increment") or "0.000000001", a.get("min_order_size") or "0.000000001",
                               exchange=a.get("exchange", ""), simulated=False)
    return out


class CryptoState(SymbolState):
    """Latest state of one pair. Sizes are in coins (no round-lot multiplier)."""
    day_start: float = 0.0  # start of the provider's current daily bar

    def apply_quote(self, raw: dict, ts: float) -> bool:
        if ts < self.quote_ts:
            return False
        self.bid, self.ask = dec(raw.get("bp")), dec(raw.get("ap"))
        self.bid_size, self.ask_size = dec(raw.get("bs")) or ZERO, dec(raw.get("as")) or ZERO
        self.quote_ts = ts
        return True

    def apply_crypto_snapshot(self, snap: dict) -> bool:
        changed = False
        q, t = snap.get("latestQuote"), snap.get("latestTrade")
        if q and (ts := parse_ts(q.get("t"))) is not None:
            changed |= self.apply_quote(q, ts)
        if t and (ts := parse_ts(t.get("t"))) is not None and ts > self.trade_ts:
            changed |= self.apply_trade(t, ts, count_volume=False)
            self.new_trade = False  # a snapshot is not a new print
        daily, prev = snap.get("dailyBar"), snap.get("prevDailyBar")
        if daily and (dts := parse_ts(daily.get("t"))) is not None:
            self.open, self.high, self.low = dec(daily.get("o")), dec(daily.get("h")), dec(daily.get("l"))
            self.volume = dec(daily.get("v")) or ZERO
            self.day_start = dts
            changed = True
        if prev and prev.get("c") is not None:
            self.prev_close = dec(prev.get("c"))
            changed = True
        return changed


def orderbook_depth(book: Optional[dict], levels: int = 10) -> Optional[dict]:
    """A provider order book -> the depth shape the UI shows (best levels first; zero sizes dropped)."""
    if not book:
        return None
    lv = lambda rows, rev: sorted(({"price": dec(r.get("p")), "size": dec(r.get("s")) or ZERO} for r in rows or []
                                   if dec(r.get("s")) and dec(r.get("p"))), key=lambda x: x["price"], reverse=rev)[:levels]
    return {"simulated": False, "source": "Alpaca", "bids": lv(book.get("b"), True), "asks": lv(book.get("a"), False),
            "as_of": parse_ts(book.get("t"))}


# ---------------------------------------------------------------------------------------------
# The feed: Alpaca stocks and options (AlpacaFeed) plus crypto pairs
# ---------------------------------------------------------------------------------------------
class AlpacaCryptoFeed(AlpacaFeed):
    """AlpacaFeed with crypto pairs added. Stocks and options behave exactly as in AlpacaFeed."""

    def __init__(self, key: str, secret: str, *, crypto_loc: str = "us",
                 crypto_connector: Optional[Callable] = None, crypto_stream_cap: int = 30, **kw):
        super().__init__(key, secret, **kw)
        if crypto_loc not in CRYPTO_LOCATIONS:
            raise ValueError(f"unknown Alpaca crypto location {crypto_loc!r}; use one of {', '.join(CRYPTO_LOCATIONS)}")
        self.crypto_loc = crypto_loc
        self.crypto_rest = CryptoREST(self.rest, crypto_loc)
        self.crypto_protocol = StreamProtocol(key, secret, cap=crypto_stream_cap)
        self.crypto_stream = StreamClient(CRYPTO_STREAM_URL.format(loc=crypto_loc), self.crypto_protocol,
                                          self._on_crypto_stream, crypto_connector or kw.get("connector")
                                          or WebsocketsConnection, sleep=kw.get("sleep", time.sleep),
                                          clock=self.wall_clock)
        self.crypto_info: dict = {}
        self._symbols: dict = {}
        self._symbols_key = None
        self.crypto_error: Optional[str] = None
        self._cstate: dict = {}
        self._cchanged: set = set()  # pairs with news since the last step (kept apart from the stock set)
        self._book_cache: dict = {}
        self._crypto_polled = 0

    # ---- symbols ------------------------------------------------------------------------------
    def symbols(self) -> dict:
        """Stocks and crypto pairs. The merged table is rebuilt only when either table is replaced
        (both are only ever swapped whole), not copied on every call."""
        if not self.crypto_info:
            return self.info
        key = (id(self.info), id(self.crypto_info))
        if self._symbols_key != key:
            self._symbols, self._symbols_key = {**self.info, **self.crypto_info}, key
        return self._symbols

    def _crypto_assets_path(self) -> Optional[Path]:
        return Path(self.data_dir) / "alpaca_crypto_assets.json" if self.data_dir else None

    def load_assets(self, max_age: float = 86400) -> int:
        n = super().load_assets(max_age)
        try:
            self.load_crypto_assets(max_age)
        except ProviderError as e:  # stocks still work without the crypto list
            self.crypto_error = f"Could not load the Alpaca crypto list: {e.message}"
            log.warning(self.crypto_error)
        return n

    def load_crypto_assets(self, max_age: float = 86400) -> int:
        path, cached = self._crypto_assets_path(), None
        if path and path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = None
        now = self.wall_clock()
        if cached and now - cached.get("fetched_at", 0) < max_age:
            self.crypto_info = cached["info"]
            return len(self.crypto_info)
        try:
            info = assets_to_info(self.crypto_rest.assets())
        except ProviderError:
            if cached:
                self.crypto_info = cached["info"]
                return len(self.crypto_info)
            raise
        self.crypto_info, self.crypto_error = info, None
        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"fetched_at": now, "info": info}), encoding="utf-8")
            except OSError:
                pass
        return len(info)

    # ---- lifecycle ------------------------------------------------------------------------------
    def start(self) -> None:
        super().start()
        self.crypto_stream.start()

    def close(self) -> None:
        super().close()
        self.crypto_stream.stop()

    # ---- quotes ---------------------------------------------------------------------------------
    def step(self, now: float) -> list:
        out = super().step(now)
        with self._lock:
            changed, self._cchanged = self._cchanged, set()
            for sym in changed:
                st = self._cstate[sym]
                q = st.to_quote()
                if q is None:
                    continue
                st.new_trade = False
                q = self._on_steps(q)
                self.quotes[sym] = q
                out.append(q)
        return out

    def _on_steps(self, q):
        """Sizes floored to the pair's quantity step, so a fill can never leave an unsellable remainder."""
        info = self.crypto_info.get(q.symbol)
        if info is None:
            return q
        step = spec_from_info(q.symbol, info).qty_increment
        return replace(q, bid_size=floor_to(q.bid_size, step), ask_size=floor_to(q.ask_size, step),
                       last_size=floor_to(q.last_size, step))

    def quote_age(self, symbol: str) -> Optional[float]:
        if symbol in self._cstate:
            st = self._cstate[symbol]
            return None if not st.received else self.wall_clock() - st.received
        return super().quote_age(symbol)

    def _on_crypto_stream(self, msgs: list) -> None:
        now = self.wall_clock()
        with self._lock:
            for m in msgs:
                sym, ts = m.get("S"), parse_ts(m.get("t"))
                if not sym or ts is None:
                    continue
                st = self._cstate.setdefault(sym, CryptoState(sym))
                if m["T"] == "q":
                    ok = st.apply_quote(m, ts)
                else:
                    ok = st.apply_trade(m, ts, count_volume=bool(st.day_start) and ts >= st.day_start)
                    if ok:
                        side = {"B": "buy", "S": "sell"}.get(m.get("tks"), "mid")  # the taker's side
                        self.prints.setdefault(sym, deque(maxlen=200)).append(
                            {"ts": ts, "price": st.last, "size": st.last_size, "side": side})
                if ok:
                    st.received = now
                    self._cchanged.add(sym)

    def apply_crypto_snapshots(self, snaps: dict) -> None:
        now = self.wall_clock()
        with self._lock:
            for sym, snap in (snaps or {}).items():
                if not snap:
                    continue
                st = self._cstate.setdefault(sym, CryptoState(sym))
                if st.apply_crypto_snapshot(snap):
                    st.received = now
                    self._cchanged.add(sym)

    # ---- polling (on the poller thread) -----------------------------------------------------------
    def poll_once(self) -> None:
        super().poll_once()
        self.poll_crypto()

    def poll_crypto(self) -> None:
        """Stream the most wanted pairs; snapshot the rest every poll interval (and streamed ones every minute)."""
        if not self.crypto_info:
            return
        now = self.wall_clock()
        pairs = [s for s in self.wanted() if s in self.crypto_info]
        self.crypto_stream.set_wanted(pairs)
        streamed = self.crypto_protocol.streamed()
        due = [s for s in pairs if now - self._last_poll.get(s, 0.0) >= (60.0 if s in streamed else self.poll_seconds)]
        self._crypto_polled = len([s for s in pairs if s not in streamed])
        for i in range(0, len(due), 100):
            batch = due[i:i + 100]
            self.apply_crypto_snapshots(self.crypto_rest.snapshots(batch, max_wait=5))
            for s in batch:
                self._last_poll[s] = now

    # ---- history and depth (called from web requests, never from the tick) -------------------------
    def bars(self, symbol: str, tf: str, limit: int = 500) -> list:
        if symbol not in self.crypto_info:
            return super().bars(symbol, tf, limit)
        if tf not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe {tf}")
        now = self.wall_clock()
        key = (symbol, tf)
        hit = self._bar_cache.get(key)
        if hit and hit[1] >= limit and now - hit[0] < TF_TTL[tf]:
            return hit[2][-limit:]
        daily = tf in ("1d", "1w")
        days = int(limit * (1.05 if tf == "1d" else 7.2)) + 3 if daily else TF_LOOKBACK_DAYS[tf]
        start = (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=days)).isoformat()
        try:
            rows = parse_bars(self.crypto_rest.bars(symbol, TF_ALPACA[tf], start, None, min(max(limit, 1), 10000),
                                                    max_wait=8), daily)
        except ProviderError as e:
            log.warning("Alpaca crypto bars %s %s: %s", symbol, tf, e.message)
            return hit[2][-limit:] if hit else []
        self._bar_cache[key] = (now, limit, rows)
        return rows[-limit:]

    def depth(self, symbol: str, levels: int = 10) -> dict:
        if symbol not in self.crypto_info:
            return super().depth(symbol, levels)
        now = self.wall_clock()
        hit = self._book_cache.get(symbol)
        if hit and now - hit[0] < 2:
            return hit[1]
        try:
            book = orderbook_depth(self.crypto_rest.latest_orderbooks([symbol], max_wait=3).get(symbol), levels)
        except ProviderError as e:
            log.warning("Alpaca crypto order book %s: %s", symbol, e.message)
            book = hit[1] if hit else None
        if book is None:
            return {"simulated": False, "source": "Alpaca", "bids": [], "asks": [],
                    "reason": "Alpaca returned no order book for this pair."}
        self._book_cache[symbol] = (now, book)
        return book

    def base_vol(self, symbol: str) -> float:
        if symbol in self.crypto_info:
            return 0.8  # only used for display; there are no crypto options
        return super().base_vol(symbol)

    # ---- status --------------------------------------------------------------------------------
    def status_info(self) -> dict:
        out = super().status_info()
        p = self.crypto_protocol
        out["crypto"] = {"source": "Alpaca", "location": self.crypto_loc, "pairs": len(self.crypto_info),
                         "note": CRYPTO_NOTE.format(loc=self.crypto_loc), "error": self.crypto_error,
                         "stream": {"state": p.phase, "message": p.error, "streamed": len(p.streamed()), "cap": p.cap,
                                    "polled": self._crypto_polled}}
        return out
