"""Alpaca market data on the free Basic plan: real-time IEX stock quotes, indicative options.

Verified facts (docs.alpaca.markets, read 2026-11-24; see docs/DATA_PROVIDERS.md):
- REST host data.alpaca.markets, stream host stream.data.alpaca.markets, assets and option
  contracts on paper-api.alpaca.markets (or api.alpaca.markets for live keys).
- Auth: headers APCA-API-KEY-ID and APCA-API-SECRET-KEY; on the stream an
  {"action":"auth","key":..,"secret":..} message after the "connected" greeting.
- Basic plan: IEX only in real time, 30 stream symbols, 1 stream connection, 200 REST calls/min.
- Option stream is msgpack-only, so option data here comes from REST snapshots.

Design: network I/O runs on two daemon threads (stream and poller) that write into a
lock-protected latest-quote map. The exchange's synchronous tick calls step(now), which
only drains that map, so a slow or dead provider can never stall order processing.
The stream carries the symbols that matter most (open orders, positions, what the UI is
showing, watchlists) up to the plan's cap; everything else is polled in batched snapshots.

This module only reads market data. It has no code path that sends an order.
"""
import json
import logging
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..core import calendar as cal
from ..core.models import Quote
from ..core.money import CENT, ZERO
from ..options import chain as optchain
from ..options import pricing
from ..options.symbology import Contract, build, parse as parse_option
from .alpaca_actions import ActionBook
from .feed import TIMEFRAMES, ClosePrice, MarketDataProvider
from .http import JsonClient, ProviderError, RateLimiter, Transport, dec, loads, parse_ts

log = logging.getLogger("paperdesk.alpaca")

DATA_URL = "https://data.alpaca.markets"
PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
STREAM_URL = "wss://stream.data.alpaca.markets/v2/{feed}"

FEEDS = {
    "iex": {"realtime": True, "delay": 0, "exchange": "IEX",
            "note": "Real-time quotes and trades from one exchange (IEX, about 2.5% of US volume). "
                    "Prices can differ slightly from the consolidated tape and volume is IEX volume only."},
    "sip": {"realtime": True, "delay": 0, "exchange": "all US exchanges (SIP)",
            "note": "Real-time consolidated quotes from all US exchanges (paid Algo Trader Plus plan)."},
    "delayed_sip": {"realtime": False, "delay": 900, "exchange": "all US exchanges (SIP)",
                    "note": "Consolidated quotes from all US exchanges, delayed 15 minutes."},
}
OPTION_FEEDS = {
    "indicative": "Alpaca indicative option feed (free: quotes are modified and trades delayed, not exchange prices)",
    "opra": "OPRA option feed (paid)",
}
TF_ALPACA = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour", "1d": "1Day", "1w": "1Week"}
TF_LOOKBACK_DAYS = {"1m": 7, "5m": 30, "15m": 60, "1h": 200}
TF_TTL = {"1m": 30, "5m": 60, "15m": 120, "1h": 300, "1d": 900, "1w": 3600}
ROUND_LOT = Decimal(100)  # stock quote sizes arrive in round lots
RECENT_TRADE = 60.0  # seconds: a missing side may be filled from a trade at most this old
# Quote age (docs/DATA_PROVIDERS.md, "Quote age"): a quote is stale when the provider's newest quote or
# trade time for the symbol is older than this in the regular session, or in the pre/post sessions...
STALE_AFTER = {"regular": 120.0, "pre": 900.0, "post": 900.0}
# ... and, while the market is closed, when it is from before the open of the latest session.


def last_session_open(now: float) -> float:
    """Open time of the most recent regular session that has started (today's, or an earlier day's)."""
    d = cal.ny_date(now)
    for _ in range(15):
        if cal.is_trading_day(d):
            t = datetime.combine(d, cal.OPEN, tzinfo=cal.NY).timestamp()
            if t <= now:
                return t
        d -= timedelta(days=1)
    return now


def is_stale(newest: float, now: float) -> bool:
    if not newest:
        return True
    limit = STALE_AFTER.get(cal.session(now))
    if limit is not None:
        return now - newest > limit
    return newest < last_session_open(now)


# ---------------------------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------------------------
class AlpacaREST:
    """Market-data endpoints plus the read-only asset and option-contract lists."""

    def __init__(self, key: str, secret: str, transport: Optional[Transport] = None,
                 limiter: Optional[RateLimiter] = None, trading_url: str = PAPER_TRADING_URL,
                 data_url: str = DATA_URL, sleep=time.sleep, wall_clock=time.time):
        self.http = JsonClient("Alpaca", {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                                          "Accept": "application/json", "User-Agent": "PaperDesk"},
                               transport, limiter or RateLimiter(190), wall_clock=wall_clock, sleep=sleep)
        self.data_url, self.trading_url = data_url, trading_url

    def _data(self, path, params=None, **kw):
        return self.http.get(self.data_url, path, params, **kw)

    def assets(self, **kw) -> list:
        return self.http.get(self.trading_url, "/v2/assets", {"status": "active", "asset_class": "us_equity"},
                             max_wait=30, **kw) or []

    def snapshots(self, symbols: Iterable[str], feed: str, **kw) -> dict:
        return self._data("/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": feed}, **kw) or {}

    def bars(self, symbol: str, timeframe: str, start: str, end: Optional[str], limit: int, feed: str,
             adjustment: str = "raw", **kw) -> list:
        j = self._data("/v2/stocks/bars", {"symbols": symbol, "timeframe": timeframe, "start": start, "end": end,
                                           "limit": limit, "feed": feed, "sort": "desc", "adjustment": adjustment},
                       **kw) or {}
        return (j.get("bars") or {}).get(symbol, [])

    def option_chain(self, underlying: str, feed: str, expiration_date: Optional[str] = None,
                     strike_gte=None, strike_lte=None, max_pages: int = 5, **kw) -> dict:
        out, token = {}, None
        for _ in range(max_pages):
            j = self._data(f"/v1beta1/options/snapshots/{underlying}",
                           {"feed": feed, "expiration_date": expiration_date, "strike_price_gte": strike_gte,
                            "strike_price_lte": strike_lte, "limit": 1000, "page_token": token}, **kw) or {}
            out.update(j.get("snapshots") or {})
            token = j.get("next_page_token")
            if not token:
                break
        return out

    def option_snapshots(self, symbols: Iterable[str], feed: str, **kw) -> dict:
        j = self._data("/v1beta1/options/snapshots", {"symbols": ",".join(symbols), "feed": feed, "limit": 1000},
                       **kw) or {}
        return j.get("snapshots") or {}

    def option_contracts(self, underlying: str, gte: str, lte: str, max_pages: int = 5, **kw) -> list:
        out, token = [], None
        for _ in range(max_pages):
            j = self.http.get(self.trading_url, "/v2/options/contracts",
                              {"underlying_symbols": underlying, "status": "active", "expiration_date_gte": gte,
                               "expiration_date_lte": lte, "limit": 10000, "page_token": token}, **kw) or {}
            out.extend(j.get("option_contracts") or [])
            token = j.get("next_page_token")
            if not token:
                break
        return out

    def news(self, symbols: str, limit: int = 20, **kw) -> list:
        j = self._data("/v1beta1/news", {"symbols": symbols, "limit": limit, "sort": "desc"}, **kw) or {}
        return j.get("news") or []

    def corporate_actions(self, symbols: str, start: str, end: str, types: Optional[str] = None,
                          max_pages: int = 5, strict: bool = False, **kw) -> dict:
        """All pages up to max_pages. With strict, a result cut off by max_pages raises instead."""
        out, token = {}, None
        for _ in range(max_pages):
            j = self._data("/v1/corporate-actions", {"symbols": symbols, "start": start, "end": end, "types": types,
                                                     "limit": 1000, "page_token": token}, **kw) or {}
            for k, rows in (j.get("corporate_actions") or {}).items():
                out.setdefault(k, []).extend(rows or [])
            token = j.get("next_page_token")
            if not token:
                break
        else:
            if token and strict:
                raise ProviderError(0, f"Alpaca returned more than {max_pages} pages of corporate actions")
        return out

    def bars_range(self, symbol: str, timeframe: str, start: str, end: str, feed: str, adjustment: str = "raw",
                   max_pages: int = 10, **kw) -> list:
        """Every bar between start and end, oldest first, following next_page_token."""
        out, token = [], None
        for _ in range(max_pages):
            j = self._data("/v2/stocks/bars", {"symbols": symbol, "timeframe": timeframe, "start": start, "end": end,
                                               "limit": 10000, "feed": feed, "sort": "asc", "adjustment": adjustment,
                                               "page_token": token}, **kw) or {}
            out.extend((j.get("bars") or {}).get(symbol, []) or [])
            token = j.get("next_page_token")
            if not token:
                return out
        raise ProviderError(0, f"Alpaca returned more than {max_pages} pages of bars for {symbol}")


# ---------------------------------------------------------------------------------------------
# Parsing (pure functions, tested against the documentation examples)
# ---------------------------------------------------------------------------------------------
def ny_day_utc(ts: float) -> int:
    """A daily bar stamped at New York midnight -> UTC midnight of that date (the chart convention)."""
    d = cal.ny_date(ts)
    return int((d - date(1970, 1, 1)).total_seconds())


def parse_bars(rows: list, daily: bool) -> list:
    """Alpaca bars (any order) -> chart bars oldest first. Chart data is float by design."""
    out = {}
    for r in rows or []:
        ts = parse_ts(r.get("t"))
        if ts is None:
            continue
        t = ny_day_utc(ts) if daily else int(ts)
        out[t] = {"time": t, "open": float(r["o"]), "high": float(r["h"]), "low": float(r["l"]),
                  "close": float(r["c"]), "volume": float(r.get("v", 0))}
    return [out[t] for t in sorted(out)]


@dataclass
class SymbolState:
    """Latest known market state for one stock, merged from snapshots and the stream."""
    symbol: str
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    bid_size: Decimal = ZERO
    ask_size: Decimal = ZERO
    last: Optional[Decimal] = None
    last_size: Decimal = ZERO
    volume: Decimal = ZERO
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    prev_close: Optional[Decimal] = None
    quote_ts: float = 0.0
    trade_ts: float = 0.0
    new_trade: bool = False
    received: float = 0.0  # wall time this process last heard about the symbol
    feed_name: str = "IEX"

    @property
    def ts(self) -> float:
        return max(self.quote_ts, self.trade_ts)

    def apply_quote(self, raw: dict, ts: float) -> bool:
        if ts < self.quote_ts:
            return False
        self.bid, self.ask = dec(raw.get("bp")), dec(raw.get("ap"))
        self.bid_size = (dec(raw.get("bs")) or ZERO) * ROUND_LOT
        self.ask_size = (dec(raw.get("as")) or ZERO) * ROUND_LOT
        self.quote_ts = ts
        return True

    def apply_trade(self, raw: dict, ts: float, count_volume: bool) -> bool:
        if ts < self.trade_ts:
            return False
        p, s = dec(raw.get("p")), dec(raw.get("s")) or ZERO
        if p is None or p <= 0:
            return False
        self.last, self.last_size, self.trade_ts, self.new_trade = p, s, ts, True
        if count_volume:
            self.volume += s
            self.high = p if self.high is None else max(self.high, p)
            self.low = p if self.low is None else min(self.low, p)
            if self.open is None:
                self.open = p
        return True

    def apply_snapshot(self, snap: dict, today: date) -> bool:
        changed = False
        q, t = snap.get("latestQuote"), snap.get("latestTrade")
        if q and (ts := parse_ts(q.get("t"))) is not None:
            changed |= self.apply_quote(q, ts)
        if t and (ts := parse_ts(t.get("t"))) is not None:
            if ts > self.trade_ts:
                changed |= self.apply_trade(t, ts, count_volume=False)
                self.new_trade = False  # a snapshot is not a new print
        daily, prev = snap.get("dailyBar"), snap.get("prevDailyBar")
        if daily and (dts := parse_ts(daily.get("t"))) is not None:
            if cal.ny_date(dts) == today:
                self.open, self.high, self.low = dec(daily["o"]), dec(daily["h"]), dec(daily["l"])
                self.volume = dec(daily.get("v")) or ZERO
                if prev:
                    self.prev_close = dec(prev.get("c"))
            else:  # before today's first trade the daily bar is the previous session
                self.prev_close = dec(daily.get("c"))
                self.open = self.high = self.low = None
                self.volume = ZERO
            changed = True
        elif prev:
            pc = dec(prev.get("c"))
            changed |= pc != self.prev_close  # before the first trade or quote this is the only price known
            self.prev_close = pc
        return changed

    def to_quote(self, now: Optional[float] = None) -> Optional[Quote]:
        """A usable Quote, or None when nothing is known yet.

        With `now` (what the feed passes) the policy is deliberate and conservative (see
        docs/DATA_PROVIDERS.md, "Missing prices"): a missing or crossed side is filled from the
        last trade only while that trade is at most RECENT_TRADE seconds old. Otherwise the
        missing side stays 0, so a market order on that side waits instead of filling at an old
        price, and `last` falls back to the last trade, then the quote midpoint, then the
        previous close (so the position is still valued). Without `now` a missing side is always
        filled from the last trade (the older behaviour, kept for callers that have no clock).
        """
        bid = self.bid if self.bid and self.bid > 0 else None
        ask = self.ask if self.ask and self.ask > 0 else None
        last = self.last
        if now is None or (last is not None and now - self.trade_ts <= RECENT_TRADE):
            if bid is None and ask is None:
                if last is None:
                    return None
                bid = ask = last
            elif bid is None:
                bid = min(ask, last) if last else ask
            elif ask is None:
                ask = max(bid, last) if last else bid
            if ask < bid:
                ask = bid
            if last is None:
                last = (bid + ask) / 2
        else:
            if bid is None and ask is None and last is None and self.prev_close is None:
                return None
            if bid is not None and ask is not None and ask < bid:
                ask = bid
            if last is None:
                last = (bid + ask) / 2 if bid is not None and ask is not None else self.prev_close or bid or ask
            bid, ask = bid or ZERO, ask or ZERO
        return Quote(symbol=self.symbol, bid=bid, ask=ask, bid_size=self.bid_size if bid else ZERO,
                     ask_size=self.ask_size if ask else ZERO, last=last,
                     last_size=self.last_size if self.new_trade else ZERO, volume=self.volume,
                     ts=self.ts, trade=self.new_trade, open=self.open, high=self.high, low=self.low,
                     prev_close=self.prev_close)

    def filled_from_trade(self, now: float) -> bool:
        """True while a missing side is being filled from a recent trade (it stops when the trade ages)."""
        one_sided = not (self.bid and self.bid > 0) or not (self.ask and self.ask > 0) or self.ask < self.bid
        return one_sided and self.last is not None and now - self.trade_ts <= RECENT_TRADE

    def note(self, now: float) -> tuple:
        """(stale, plain sentence or None) describing where the shown price comes from."""
        has_bid, has_ask = bool(self.bid and self.bid > 0), bool(self.ask and self.ask > 0)
        today = cal.ny_date(now)
        if self.last is None:
            if has_bid and has_ask:
                return False, "No trade yet; the price shown is the quote midpoint."
            if self.prev_close is not None:
                return True, "No trade or two-sided quote yet; the price shown is the previous close."
        stale = bool(self.trade_ts) and cal.ny_date(self.trade_ts) != today
        if self.last is not None and now - self.trade_ts <= RECENT_TRADE and not (has_bid and has_ask):
            side = "bid" if not has_bid and has_ask else "ask" if has_bid else "bid and ask"
            return False, f"The {side} shown {'is' if side != 'bid and ask' else 'are'} the last trade price: " \
                          f"the {self.feed_name} book has no {side} right now and the trade is under " \
                          f"{RECENT_TRADE:.0f} seconds old."
        missing = [n for n, ok in (("bid", has_bid), ("ask", has_ask)) if not ok]
        when = (f" Last trade {cal.ny_datetime(self.trade_ts):%Y-%m-%d %H:%M} New York time."
                if self.trade_ts else "")
        if missing:
            return True, (f"No {' or '.join(missing)} on {self.feed_name} right now, so market orders on that side "
                          f"wait.{when}")
        if stale:
            return True, f"No trade today yet; the last price is from an earlier session.{when}"
        return False, None

def option_quote_from_snapshot(symbol: str, snap: dict) -> Optional[Quote]:
    q, t = snap.get("latestQuote") or {}, snap.get("latestTrade") or {}
    bid, ask = dec(q.get("bp")) or ZERO, dec(q.get("ap")) or ZERO
    last = dec(t.get("p"))
    if ask <= 0 and not last:
        return None
    if ask < bid:
        ask = bid
    mid = (bid + ask) / 2
    return Quote(symbol=symbol, bid=bid, ask=ask, bid_size=dec(q.get("bs")) or ZERO, ask_size=dec(q.get("as")) or ZERO,
                 last=last if last else max(mid, CENT), ts=max(parse_ts(q.get("t")) or 0.0, parse_ts(t.get("t")) or 0.0))


def option_leg(c: Contract, snap: Optional[dict], spot: float, base_vol: float, now: float, source: str,
               open_interest=None) -> dict:
    """One side of a chain row: provider data where it exists, the model for the rest."""
    model = optchain.option_snapshot(c, spot, base_vol, now)
    q = option_quote_from_snapshot(c.symbol, snap) if snap else None
    if q is None:
        return {**model, "source": "model", "greeks_source": "model"}
    T = max(c.expiry_ts - now, 0.0) / optchain.YEAR_SECONDS
    iv = snap.get("impliedVolatility")
    iv = float(iv) if iv is not None else pricing.implied_vol(float(q.mid), spot, float(c.strike), T,
                                                             optchain.RISK_FREE, c.kind)
    greeks = snap.get("greeks") or {}
    if all(k in greeks for k in ("delta", "gamma", "theta", "vega", "rho")):
        g, gsrc = {k: float(greeks[k]) for k in ("delta", "gamma", "theta", "vega", "rho")}, source
    else:
        g = pricing.greeks(spot, float(c.strike), T, optchain.RISK_FREE, iv or model["iv"], c.kind)
        gsrc = "model"
    daily = snap.get("dailyBar") or {}
    return {**model, "bid": q.bid, "ask": q.ask, "mid": (q.bid + q.ask) / 2, "last": q.last,
            "bid_size": q.bid_size, "ask_size": q.ask_size, "iv": iv if iv is not None else model["iv"],
            "volume": int(daily["v"]) if daily.get("v") is not None else None,
            "open_interest": int(open_interest) if open_interest not in (None, "") else None,
            "quote_time": q.ts, "source": source, "greeks_source": gsrc, **g}


def build_chain(underlying: str, expiry: date, snaps: dict, spot: float, base_vol: float, now: float,
                source: str, open_interest: Optional[dict] = None, strikes=()) -> list:
    """Rows for every strike with a provider snapshot, plus every listed strike in `strikes` (model-priced
    where the provider sent nothing)."""
    by_strike: dict = {k: {} for k in strikes}
    for sym, snap in snaps.items():
        c = parse_option(sym)
        if c is None or c.expiry != expiry or c.underlying != underlying:
            continue
        by_strike.setdefault(c.strike, {})[c.kind] = snap
    rows = []
    oi = open_interest or {}
    for k in sorted(by_strike):
        row = {"strike": k}
        for kind, key in (("C", "call"), ("P", "put")):
            c = Contract(build(underlying, expiry, kind, k), underlying, expiry, kind, k)
            row[key] = option_leg(c, by_strike[k].get(kind), spot, base_vol, now, source, oi.get(c.symbol))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------------------------
# Stream protocol (pure state machine) and the thread that runs it
# ---------------------------------------------------------------------------------------------
class StreamProtocol:
    """Alpaca stream handshake and subscription bookkeeping, with no I/O.

    handle() takes the decoded frames and returns (messages to send, data messages, action),
    where action is None, "reconnect" or "stop".
    """

    def __init__(self, key: str, secret: str, cap: int = 30):
        self._key, self._secret = key, secret
        self.cap = cap
        self.phase = "idle"  # idle, connecting, authenticating, ready, failed
        self.error: Optional[str] = None
        self.fatal = False
        self.retry_after = 0.0
        self.wanted: list = []
        self.subscribed: set = set()
        self.pending = 0
        self.last_message = 0.0

    def connecting(self) -> None:
        self.phase, self.subscribed, self.pending = "connecting", set(), 0

    def disconnected(self, reason: str) -> None:
        if self.phase != "failed":
            self.phase = "idle"
            self.error = reason
        self.subscribed, self.pending = set(), 0

    def target(self) -> set:
        return set(self.wanted[:self.cap])

    def streamed(self) -> set:
        """Symbols the server has confirmed on this connection."""
        return set(self.subscribed) if self.phase == "ready" else set()

    def set_wanted(self, symbols: Iterable[str]) -> list:
        self.wanted = list(dict.fromkeys(symbols))
        return self._sync()

    def _sync(self) -> list:
        if self.phase != "ready" or self.pending:
            return []
        tgt = self.target()
        add, remove = tgt - self.subscribed, self.subscribed - tgt
        out = []
        if remove:
            out.append({"action": "unsubscribe", "trades": sorted(remove), "quotes": sorted(remove)})
        if add:
            out.append({"action": "subscribe", "trades": sorted(add), "quotes": sorted(add)})
        self.pending = len(out)
        return out

    def auth_message(self) -> dict:
        return {"action": "auth", "key": self._key, "secret": self._secret}

    def handle(self, frames: list, now: float) -> tuple:
        out, data, action = [], [], None
        self.last_message = now
        for m in frames:
            if not isinstance(m, dict):
                continue
            kind = m.get("T")
            if kind in ("q", "t"):
                data.append(m)
            elif kind == "success":
                if m.get("msg") == "connected":
                    self.phase = "authenticating"
                    out.append(self.auth_message())
                elif m.get("msg") == "authenticated":
                    self.phase, self.error, self.fatal = "ready", None, False
                    out.extend(self._sync())
            elif kind == "subscription":
                self.subscribed = set(m.get("quotes") or []) | set(m.get("trades") or [])
                self.pending = max(0, self.pending - 1)
                out.extend(self._sync())
            elif kind == "error":
                a = self._error(int(m.get("code") or 0), str(m.get("msg") or ""))
                action = action or a
                if a is None:
                    out.extend(self._sync())
        return out, data, action

    def _error(self, code: int, msg: str) -> Optional[str]:
        if code == 402:
            self.phase, self.fatal = "failed", True
            self.error = ("Alpaca rejected the API key on the stream (auth failed). Check APCA_API_KEY_ID and "
                          "APCA_API_SECRET_KEY; keys from a reset paper account must be regenerated.")
            return "stop"
        if code == 409:
            self.phase, self.fatal = "failed", True
            self.error = "Your Alpaca plan does not include this data feed (insufficient subscription)."
            return "stop"
        if code == 406:
            self.phase = "failed"
            self.error = ("Alpaca allows one stream connection per key and another program is using it "
                          "(connection limit exceeded). Showing polled prices until it is free.")
            self.retry_after = 60.0
            return "reconnect"
        if code == 405:
            self.pending = 0
            self.cap = max(1, len(self.subscribed) if self.subscribed else self.cap // 2)
            self.error = (f"Alpaca's symbol limit was reached; streaming {self.cap} symbols and polling the rest.")
            return None
        if code == 401:
            self.phase = "authenticating"
            return None
        if code in (404, 407, 500):
            self.error = f"Alpaca stream error {code}: {msg}"
            return "reconnect"
        self.error = f"Alpaca stream error {code}: {msg}"
        self.pending = 0
        return None


class Connection:
    """What StreamClient needs from a websocket: send text, receive with a timeout, close."""

    def send(self, text: str) -> None: ...
    def recv(self, timeout: float) -> Optional[str]: ...  # None on timeout; raises on close
    def close(self) -> None: ...


class WebsocketsConnection(Connection):
    """websockets' threaded client: pure Python, works the same on Windows, macOS and Linux."""

    def __init__(self, url: str):
        from websockets.sync.client import connect
        self._ws = connect(url, open_timeout=15, close_timeout=3, max_size=2 ** 24)

    def send(self, text: str) -> None:
        self._ws.send(text)

    def recv(self, timeout: float) -> Optional[str]:
        try:
            msg = self._ws.recv(timeout=timeout)
        except TimeoutError:
            return None
        return msg.decode("utf-8") if isinstance(msg, bytes) else msg

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


class StreamClient:
    """Runs StreamProtocol over a Connection on a daemon thread, reconnecting with backoff."""

    def __init__(self, url: str, protocol: StreamProtocol, on_data: Callable[[list], None],
                 connector: Callable[[str], Connection] = WebsocketsConnection,
                 sleep=time.sleep, clock=time.time):
        self.url, self.protocol, self.on_data, self.connector = url, protocol, on_data, connector
        self.sleep, self.clock = sleep, clock
        self.lock = threading.Lock()
        self._wanted: Optional[list] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connects = 0

    def set_wanted(self, symbols: list) -> None:
        with self.lock:
            self._wanted = list(symbols)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self.run_forever, name="alpaca-stream", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            action = self.run_once()
            if action == "stop" or self._stop.is_set():
                return
            wait = max(self.protocol.retry_after, backoff) * (1 + random.random() * 0.2)
            self.protocol.retry_after = 0.0
            backoff = 1.0 if self.protocol.phase == "ready" else min(backoff * 2, 60.0)
            self._stop.wait(wait)

    def run_once(self, max_frames: Optional[int] = None) -> Optional[str]:
        """One connection's life. Returns the action that ended it."""
        self.protocol.connecting()
        try:
            conn = self.connector(self.url)
        except Exception as e:
            self.protocol.disconnected(f"Could not connect to the Alpaca stream: {e}")
            return "reconnect"
        self.connects += 1
        frames = 0
        try:
            while not self._stop.is_set():
                with self.lock:
                    wanted, self._wanted = self._wanted, None
                if wanted is not None:
                    self._send(conn, self.protocol.set_wanted(wanted))
                text = conn.recv(1.0)
                if text is None:
                    continue
                frames += 1
                try:
                    msgs = loads(text)
                except ValueError:
                    continue
                out, data, action = self.protocol.handle(msgs if isinstance(msgs, list) else [msgs], self.clock())
                self._send(conn, out)
                if data:
                    self.on_data(data)
                if action:
                    self.protocol.disconnected(self.protocol.error or "disconnected")
                    return action
                if max_frames is not None and frames >= max_frames:
                    return None
        except Exception as e:  # closed by the server or the network dropped
            self.protocol.disconnected(f"The Alpaca stream disconnected ({type(e).__name__}); reconnecting.")
            return "reconnect"
        finally:
            conn.close()
        return None

    @staticmethod
    def _send(conn: Connection, msgs: list) -> None:
        for m in msgs:
            conn.send(json.dumps(m))


# ---------------------------------------------------------------------------------------------
# The feed
# ---------------------------------------------------------------------------------------------
class AlpacaFeed(MarketDataProvider):
    name = "alpaca"
    simulated = False
    late_actions = True  # Alpaca can publish a corporate action after its ex-date

    def __init__(self, key: str, secret: str, *, feed: str = "iex", options_feed: str = "indicative",
                 trading_url: str = PAPER_TRADING_URL, transport: Optional[Transport] = None,
                 connector: Optional[Callable[[str], Connection]] = None, rpm: float = 190,
                 data_dir: Optional[str] = None, poll_seconds: float = 5.0, stream_cap: int = 30,
                 sleep=time.sleep, wall_clock=time.time):
        super().__init__()
        if feed not in FEEDS:
            raise ValueError(f"unknown Alpaca feed {feed!r}; use one of {', '.join(FEEDS)}")
        if options_feed not in OPTION_FEEDS:
            raise ValueError(f"unknown Alpaca option feed {options_feed!r}; use indicative or opra")
        self.data_feed, self.options_feed = feed, options_feed
        self.delay_seconds = FEEDS[feed]["delay"]
        self.wall_clock = wall_clock
        self.rest = AlpacaREST(key, secret, transport, RateLimiter(rpm), trading_url, sleep=sleep,
                               wall_clock=wall_clock)
        self.protocol = StreamProtocol(key, secret, cap=stream_cap)
        self.stream = StreamClient(STREAM_URL.format(feed=feed), self.protocol, self._on_stream,
                                   connector or WebsocketsConnection, sleep=sleep, clock=wall_clock)
        self.data_dir = data_dir
        self.poll_seconds = poll_seconds
        self._lock = threading.RLock()
        self._state: dict = {}
        self._changed: set = set()
        self._opt: dict = {}  # option symbol -> (Quote, received wall time)
        self._bar_cache: dict = {}
        self._chain_cache: dict = {}
        self._exp_cache: dict = {}
        self._oi: dict = {}
        self._last_poll: dict = {}
        self._ondemand: dict = {}
        self.interest_source: Optional[Callable[[], list]] = None
        self.holdings_source: Optional[Callable[[], list]] = None  # symbols held or with open orders
        self.actions = ActionBook(self.rest, wall_clock, lambda: self.holdings_source() if self.holdings_source else [])
        self._listing: dict = {}  # underlying -> {expiry: set of listed strikes} (standard contracts only)
        self._fresh_until: dict = {}  # symbol -> wall time a side filled from a recent trade stops being valid
        self._splits: dict = {}  # symbol -> {action id: (ex_date, old/new)} for previous-close consistency
        self._closes: dict = {}  # (symbol, date) -> ClosePrice, filled by the poller (closing_price)
        self._close_retry: dict = {}  # (symbol, date) -> wall time of the next attempt after a failure
        self.extra_interest: Optional[Callable[[], list]] = None
        self.assets_error: Optional[str] = None
        self.assets_loaded_at: Optional[float] = None
        self._polled_count = 0
        self._stop = threading.Event()
        self._poller: Optional[threading.Thread] = None

    # ---- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        self.stream.start()
        if self._poller is None:
            self._poller = threading.Thread(target=self._poll_forever, name="alpaca-poller", daemon=True)
            self._poller.start()

    def close(self) -> None:
        self._stop.set()
        self.stream.stop()

    # ---- symbol list -------------------------------------------------------------------------
    def _assets_path(self) -> Optional[Path]:
        return Path(self.data_dir) / "alpaca_assets.json" if self.data_dir else None

    def load_assets(self, max_age: float = 86400) -> int:
        """Fill the symbol list from a fresh cache file or the assets endpoint. Raises ProviderError."""
        path, cached = self._assets_path(), None
        if path and path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = None
        now = self.wall_clock()
        if cached and now - cached.get("fetched_at", 0) < max_age:
            self._set_assets(cached["assets"], cached["fetched_at"])
            return len(self.info)
        try:
            rows = self.rest.assets()
        except ProviderError as e:
            self.assets_error = f"Could not load the Alpaca symbol list: {e.message}"
            if cached:  # an old list beats none
                self._set_assets(cached["assets"], cached["fetched_at"])
                return len(self.info)
            raise
        assets = [{"symbol": a["symbol"], "name": a.get("name") or a["symbol"], "exchange": a.get("exchange", "")}
                  for a in rows if a.get("tradable") and a.get("status", "active") == "active"
                  and a.get("exchange") not in ("OTC", "CRYPTO") and a.get("class", "us_equity") == "us_equity"]
        self._set_assets(assets, now)
        self.assets_error = None
        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"fetched_at": now, "assets": assets}), encoding="utf-8")
            except OSError:
                pass
        return len(self.info)

    def _set_assets(self, assets: list, fetched_at: float) -> None:
        info = {}
        for a in assets:
            name = a["name"]
            kind = "etf" if "ETF" in name.upper().replace(",", " ").split() else "stock"  # Alpaca has no fund flag
            info[a["symbol"]] = {"symbol": a["symbol"], "name": name, "sector": "", "exchange": a.get("exchange", ""),
                                 "type": kind}
        self.info = info
        self.assets_loaded_at = fetched_at

    # ---- quotes --------------------------------------------------------------------------------
    def request(self, symbol: str, seconds: float = 120) -> None:
        """Ask the poller to fetch a symbol soon even if nothing else needs it."""
        self._ondemand[symbol] = self.wall_clock() + seconds

    def step(self, now: float) -> list:
        wall = self.wall_clock()
        with self._lock:
            changed, self._changed = self._changed, set()
            for sym, until in list(self._fresh_until.items()):
                if wall > until:  # the trade that filled a missing side is too old now: show the side as empty
                    self._fresh_until.pop(sym, None)
                    changed.add(sym)
            out = []
            for sym in changed:
                st = self._state.get(sym)
                q = st.to_quote(wall) if st else None
                if q is None:
                    continue
                if st.filled_from_trade(wall):
                    self._fresh_until[sym] = st.trade_ts + RECENT_TRADE
                st.new_trade = False
                q.stale = is_stale(st.ts, wall)
                self.quotes[sym] = q
                out.append(q)
        return out

    def quote(self, symbol: str) -> Optional[Quote]:
        """The latest quote, with `stale` worked out now (a quote ages while nothing new arrives)."""
        q = self.quotes.get(symbol)
        st = self._state.get(symbol)
        if q is None or st is None:
            return q
        stale = is_stale(st.ts, self.wall_clock())
        if q.stale != stale:
            q = self.quotes[symbol] = replace(q, stale=stale)
        return q

    def quote_note(self, symbol: str) -> Optional[dict]:
        """{"stale": bool, "note": text} when the shown price needs explaining, else None."""
        st = self._state.get(symbol)
        if st is None:
            return None
        now = self.wall_clock()
        stale, text = st.note(now)
        old = is_stale(st.ts, now)
        if old and st.ts and cal.session(now) != "closed":
            age = now - st.ts
            when = f"{age / 60:.0f} minutes" if age < 7200 else f"{age / 3600:.0f} hours"
            text = ((text + " ") if text else "") + (f"The latest quote or trade for {symbol} is {when} old, so it is "
                                                     f"treated as stale: market orders are refused and orders do not "
                                                     f"fill against it until a new quote arrives.")
        return {"stale": stale or old, "note": text} if text else None

    def no_price_reason(self, symbol: str) -> str:
        if parse_option(symbol):
            return (f"Alpaca's {self.options_feed} option feed has no quote for {symbol} and the model has no "
                    f"underlying price to work from.")
        if symbol not in self.info:
            return f"{symbol} is not in Alpaca's list of tradable US equities."
        return (f"No quote has arrived from Alpaca for {symbol} yet. It may be halted, not trading on "
                f"{FEEDS[self.data_feed]['exchange']} yet today, or the first request is still in progress.")

    def quote_age(self, symbol: str) -> Optional[float]:
        st = self._state.get(symbol)
        return None if st is None or not st.received else self.wall_clock() - st.received

    def _on_stream(self, msgs: list) -> None:
        now = self.wall_clock()
        today = cal.ny_date(now)
        with self._lock:
            for m in msgs:
                sym = m.get("S")
                ts = parse_ts(m.get("t"))
                if not sym or ts is None:
                    continue
                st = self._state_for(sym)
                if m["T"] == "q":
                    ok = st.apply_quote(m, ts)
                else:
                    ok = st.apply_trade(m, ts, count_volume=cal.ny_date(ts) == today)
                    if ok:
                        side = "buy" if st.ask and st.last >= st.ask else "sell" if st.bid and st.last <= st.bid else "mid"
                        self.prints.setdefault(sym, deque(maxlen=200)).append(
                            {"ts": ts, "price": st.last, "size": st.last_size, "side": side})
                if ok:
                    st.received = now
                    self._changed.add(sym)

    def _state_for(self, sym: str) -> SymbolState:
        st = self._state.get(sym)
        if st is None:
            st = self._state[sym] = SymbolState(sym, feed_name=FEEDS[self.data_feed]["exchange"].split(" ")[0])
        return st

    def apply_snapshots(self, snaps: dict) -> None:
        now = self.wall_clock()
        today = cal.ny_date(now)
        with self._lock:
            for sym, snap in (snaps or {}).items():
                if not snap:
                    continue
                st = self._state_for(sym)
                if st.apply_snapshot(snap, today):
                    self._fix_prev_close(sym, st, snap, today)
                    st.received = now
                    self._changed.add(sym)

    def _fix_prev_close(self, sym: str, st: SymbolState, snap: dict, today: date) -> None:
        """Keep the previous close on the same basis as today's prices after a split.

        Whether Alpaca's snapshot adjusts the previous daily bar for a split on today's ex-date is
        UNVERIFIED. So when a split took effect after the bar the previous close came from, the
        raw and the split-adjusted value are both candidates, and the one closer to today's prices
        (last trade or quote) is kept. A split ratio is far from 1, so the two are easy to tell apart.
        """
        splits = self._splits.get(sym)
        if not splits or st.prev_close is None:
            return
        daily, prev = snap.get("dailyBar"), snap.get("prevDailyBar")
        dts = parse_ts(daily.get("t")) if daily else None
        bar = prev if dts is not None and cal.ny_date(dts) == today else daily if dts is not None else prev
        bts = parse_ts((bar or {}).get("t"))
        if bts is None:
            return
        bar_day = cal.ny_date(bts)
        factor = Decimal(1)
        for ex, f in splits.values():
            if bar_day < ex <= today:
                factor *= f
        if factor == 1:
            return
        if st.last is not None and st.trade_ts and cal.ny_date(st.trade_ts) == today:
            ref = st.last
        elif st.bid and st.ask and st.bid > 0 and st.ask > 0 and st.quote_ts and cal.ny_date(st.quote_ts) == today:
            ref = (st.bid + st.ask) / 2
        else:
            ref = None  # nothing from today yet: assume the bar is not adjusted; the next snapshot re-checks
        adjusted = (st.prev_close * factor).quantize(Decimal("0.0001"))
        if ref is None or abs(math.log(float(adjusted / ref))) < abs(math.log(float(st.prev_close / ref))):
            st.prev_close = adjusted

    def option_quote(self, symbol: str) -> Optional[Quote]:
        hit = self._opt.get(symbol)
        if hit is None:
            return None
        q, received = hit
        if self.wall_clock() - received > 900:
            return None
        return q

    def _store_option_snaps(self, snaps: dict) -> None:
        now = self.wall_clock()
        for sym, snap in (snaps or {}).items():
            q = option_quote_from_snapshot(sym, snap) if snap else None
            if q is not None:
                self._opt[sym] = (q, now)

    # ---- interest and polling --------------------------------------------------------------------
    def wanted(self) -> list:
        out = []
        for src in (self.interest_source, self.extra_interest):
            if src is None:
                continue
            try:
                out.extend(src())
            except Exception:  # the exchange changed under us; try again next round
                pass
        now = self.wall_clock()
        for s, until in list(self._ondemand.items()):
            if until < now:
                self._ondemand.pop(s, None)
            else:
                out.append(s)
        return list(dict.fromkeys(s for s in out if s))[:400]

    def poll_once(self) -> None:
        """One round of interest tracking and REST polling. Called by the poller thread and by tests."""
        now = self.wall_clock()
        wanted = self.wanted()
        options = [s for s in wanted if parse_option(s)]
        stocks = list(dict.fromkeys([s for s in wanted if not parse_option(s) and s in self.info] +
                                    [parse_option(s).underlying for s in options
                                     if parse_option(s).underlying in self.info]))
        self.stream.set_wanted(stocks)
        streamed = self.protocol.streamed()
        closed = cal.session(now) == "closed"
        every = 60.0 if closed else self.poll_seconds
        due = []
        for s in stocks:
            last = self._last_poll.get(s, 0.0)
            interval = 60.0 if s in streamed else every  # streamed symbols still refresh daily OHLC
            if now - last >= interval:
                due.append(s)
        self._polled_count = len([s for s in stocks if s not in streamed])
        for i in range(0, len(due), 100):
            batch = due[i:i + 100]
            self.apply_snapshots(self.rest.snapshots(batch, self.data_feed, max_wait=5))
            for s in batch:
                self._last_poll[s] = now
        odue = [s for s in options if now - self._last_poll.get(s, 0.0) >= every]
        for i in range(0, len(odue), 100):
            batch = odue[i:i + 100]
            self._store_option_snaps(self.rest.option_snapshots(batch, self.options_feed, max_wait=5))
            for s in batch:
                self._last_poll[s] = now

    def _poll_forever(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.info:
                    self.load_assets()
                self.poll_once()
            except ProviderError as e:
                log.warning("Alpaca polling: %s", e.message)
            except Exception as e:  # never let the poller die
                log.warning("Alpaca polling failed: %r", e)
            try:
                self.actions.poll()  # corporate actions for the lifecycle; the tick only reads the result
            except Exception as e:
                log.warning("Alpaca corporate actions failed: %r", e)
            try:
                self.fetch_closes()  # closing prices asked for through closing_price()
            except Exception as e:
                log.warning("Alpaca closing prices failed: %r", e)
            self._stop.wait(1.0)

    # ---- history ---------------------------------------------------------------------------------
    def bars(self, symbol: str, tf: str, limit: int = 500) -> list:
        if tf not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe {tf}")
        now = self.wall_clock()
        key = (symbol, tf)
        hit = self._bar_cache.get(key)
        if hit and hit[1] >= limit and now - hit[0] < TF_TTL[tf]:
            return hit[2][-limit:]
        try:
            rows = self._fetch_bars(symbol, tf, limit, now)
        except ProviderError as e:
            log.warning("Alpaca bars %s %s: %s", symbol, tf, e.message)
            return hit[2][-limit:] if hit else []
        self._bar_cache[key] = (now, limit, rows)
        return rows[-limit:]

    def _fetch_bars(self, symbol: str, tf: str, limit: int, now: float) -> list:
        daily = tf in ("1d", "1w")
        if daily:
            days = int(limit * (1.5 if tf == "1d" else 7.5)) + 10
            start = (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=days)).date().isoformat()
        else:
            start = (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=TF_LOOKBACK_DAYS[tf])).isoformat()
        start = max(start, "2016-01-01")
        n = min(max(limit, 1), 10000)
        if daily and self.data_feed == "iex":
            # Consolidated (SIP) history is free when it ends at least 15 minutes ago; it has full volume.
            end = datetime.fromtimestamp(now - 16 * 60, timezone.utc).isoformat()
            try:
                return parse_bars(self.rest.bars(symbol, TF_ALPACA[tf], start, end, n, "sip", "split", max_wait=8), True)
            except ProviderError as e:
                if e.status not in (400, 403, 422):
                    raise
        # Split-adjusted at every timeframe, so a chart that spans a split has no jump in it.
        return parse_bars(self.rest.bars(symbol, TF_ALPACA[tf], start, None, n, self.data_feed,
                                         "split", max_wait=8), daily)

    def base_vol(self, symbol: str) -> float:
        hit = self._bar_cache.get((symbol, "1d"))
        closes = [b["close"] for b in hit[2][-61:]] if hit else []
        if len(closes) >= 21:
            rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
            return min(max(math.sqrt(var * 252), 0.05), 3.0)
        return 0.3

    # ---- options -------------------------------------------------------------------------------
    def option_expirations(self, underlying: str) -> Optional[list]:
        listing = self.option_listing(underlying)
        return sorted(listing) if listing else None

    def option_listing(self, underlying: str, fetch: bool = True) -> Optional[dict]:
        """{expiry: set of strikes} of the standard (100-share) contracts Alpaca lists, or None if unknown.

        Cached for an hour; a failed refresh keeps the previous list. Adjusted contracts (a root that
        differs from the underlying, or a deliverable other than 100 shares) are left out: the
        simulator only knows standard contracts.
        """
        now = self.wall_clock()
        hit = self._exp_cache.get(underlying)
        if hit and (now - hit[0] < 3600 or not fetch):
            return hit[1]
        if not fetch:
            return None
        today = cal.ny_date(now)
        try:
            rows = self.rest.option_contracts(underlying, today.isoformat(), (today + timedelta(days=800)).isoformat(),
                                              max_wait=8)
        except ProviderError as e:
            log.warning("Alpaca option contracts %s: %s", underlying, e.message)
            return hit[1] if hit else None
        listing: dict = {}
        for r in rows:
            c = parse_option(r.get("symbol") or "")
            if c is None or c.underlying != underlying:
                continue
            if str(r.get("size") or "100") != "100" or str(r.get("multiplier") or "100") != "100":
                continue
            if r.get("root_symbol") not in (None, "", underlying) or r.get("tradable") is False:
                continue
            listing.setdefault(c.expiry, set()).add(c.strike)
            if r.get("open_interest") not in (None, ""):
                self._oi[r["symbol"]] = r["open_interest"]
        if not listing:
            return hit[1] if hit else None
        self._exp_cache[underlying] = (now, listing)
        return listing

    def option_chain(self, underlying: str, expiry: date, now: float) -> Optional[dict]:
        wall = self.wall_clock()
        key = (underlying, expiry)
        hit = self._chain_cache.get(key)
        if hit and wall - hit[0] < 10:
            return hit[1]
        q = self.quotes.get(underlying)
        if q is None:
            try:
                self.apply_snapshots(self.rest.snapshots([underlying], self.data_feed, max_wait=5))
                self.step(now)
            except ProviderError:
                pass
            q = self.quotes.get(underlying)
        if q is None:
            return None
        spot = float(q.last)
        listed = (self.option_listing(underlying) or {}).get(expiry)
        strikes = {k for k in listed if spot * 0.7 <= float(k) <= spot * 1.3} if listed else set()
        try:
            snaps = self.rest.option_chain(underlying, self.options_feed, expiry.isoformat(),
                                           strike_gte=f"{spot * 0.7:.2f}", strike_lte=f"{spot * 1.3:.2f}", max_wait=8)
        except ProviderError as e:
            log.warning("Alpaca option chain %s: %s", underlying, e.message)
            if hit:
                return hit[1]
            snaps = {}
        if not snaps and not strikes:
            return None
        self._store_option_snaps(snaps)
        rows = build_chain(underlying, expiry, snaps, spot, self.base_vol(underlying), now, self.options_feed, self._oi,
                           strikes=strikes)
        if not rows:
            return None
        model_legs = sum(1 for r in rows for k in ("call", "put") if r[k]["source"] == "model")
        if model_legs == 2 * len(rows):  # listed contracts, but no option quote at all
            source, label, simulated = "model", ("model prices on the strikes Alpaca lists (no option quotes were "
                                                 "received), not market data"), True
        else:
            source, label, simulated = f"alpaca-{self.options_feed}", OPTION_FEEDS[self.options_feed], False
        out = {"underlying": underlying, "expiry": expiry.isoformat(), "spot": spot, "simulated": simulated,
               "source": source, "source_label": label,
               "as_of": wall, "model_legs": model_legs, "rows": rows}
        self._chain_cache[key] = (wall, out)
        return out

    # ---- official closes ----------------------------------------------------------------------
    def closing_price(self, symbol: str, day: date) -> ClosePrice:
        """Official (unadjusted) close of symbol on day, from Alpaca's daily bar. Never blocks: the first call
        queues the request for the poller thread and returns ClosePrice(False, None, ...); later calls return
        the cached result. Cached for good once known; a failure is retried after 60 s."""
        with self._lock:
            hit = self._closes.get((symbol, day))
            if hit is not None and hit.ready:
                return hit
            if hit is None:
                self._closes[(symbol, day)] = hit = ClosePrice(False, None, "requested; Alpaca has not answered yet")
            return hit

    def fetch_closes(self) -> int:
        """Fetch the closes asked for (poller thread). Returns how many were resolved."""
        now = self.wall_clock()
        with self._lock:
            todo = [k for k, v in self._closes.items() if not v.ready and self._close_retry.get(k, 0.0) <= now]
        done = 0
        for sym, day in todo:
            if cal.is_trading_day(day) and now < cal.close_ts(day) + 16 * 60:
                continue  # not final yet (and consolidated data needs to be 15 minutes old)
            try:
                rows, feed = [], "sip" if self.data_feed == "iex" else self.data_feed
                try:
                    rows = self.rest.bars_range(sym, "1Day", day.isoformat(), (day + timedelta(days=1)).isoformat(),
                                                feed, "raw", max_wait=30)
                except ProviderError as e:
                    if feed != "sip" or e.status not in (400, 403, 422):
                        raise
                    rows = self.rest.bars_range(sym, "1Day", day.isoformat(), (day + timedelta(days=1)).isoformat(),
                                                "iex", "raw", max_wait=30)
                    feed = "iex"
            except ProviderError as e:
                with self._lock:
                    self._closes[(sym, day)] = ClosePrice(False, None, f"retrying: {e.message}")
                    self._close_retry[(sym, day)] = now + 60.0
                continue
            bar = next((r for r in rows if (t := parse_ts(r.get("t"))) is not None and cal.ny_date(t) == day), None)
            res = (ClosePrice(True, dec(bar["c"]), f"Alpaca {feed.upper()} daily bar") if bar is not None
                   else ClosePrice(True, None, f"Alpaca has no daily bar for {sym} on {day.isoformat()}"))
            with self._lock:
                self._closes[(sym, day)] = res
            done += 1
        return done

    # ---- corporate actions ---------------------------------------------------------------------
    def corporate_actions(self, start, end) -> list:
        """Ready actions for held symbols (never blocks). Raises ActionsPending until fetched; see alpaca_actions."""
        return self.actions.actions_for(start, end)

    def unsupported_actions(self, start, end) -> list:
        return self.actions.notices_for(start, end)

    def margin_holds(self, today, applied=(), since=None) -> dict:
        return self.actions.holds(today, set(applied), since)

    def _adjust_for_action(self, action) -> None:
        """A split took effect. Alpaca's bars are requested split-adjusted, so the cached bars are dropped
        and fetched again (already adjusted); the current quote, prints and stored state are scaled so
        nothing shows the old price until the next update; the previous close is kept on the adjusted
        basis (see _fix_prev_close); option listings and quotes on the old strikes are dropped."""
        if action.kind != "split":
            return
        sym = action.symbol
        f = Decimal(action.old) / Decimal(action.new)
        with self._lock:
            st = self._state.get(sym)
            if st is not None:
                for k in ("bid", "ask", "last", "open", "high", "low", "prev_close"):
                    v = getattr(st, k)
                    if v is not None and v > 0:  # an empty side stays empty: never invent a price
                        setattr(st, k, (v * f).quantize(Decimal("0.0001")))
                st.volume = st.volume / f
                q = st.to_quote(self.wall_clock())
                if q is not None:
                    self.quotes[sym] = q
            for p in self.prints.get(sym, ()):
                p["price"] = (p["price"] * f).quantize(Decimal("0.0001"))
            self._splits.setdefault(sym, {})[action.id] = (action.ex_date, f)
            for key in [k for k in self._bar_cache if k[0] == sym]:
                self._bar_cache.pop(key, None)
            for key in [k for k in self._chain_cache if k[0] == sym]:
                self._chain_cache.pop(key, None)
            self._exp_cache.pop(sym, None)
            for o in [o for o in self._opt if (c := parse_option(o)) is not None and c.underlying == sym]:
                self._opt.pop(o, None)

    def snapshot(self) -> dict:
        """Saved in market.json: which corporate actions the feed has already reflected, and the splits
        kept for previous-close consistency (so a restart neither re-applies nor forgets them)."""
        return {"applied_actions": sorted(self.applied_actions),
                "splits": {s: {i: [ex.isoformat(), str(f)] for i, (ex, f) in v.items()} for s, v in self._splits.items()}}

    def restore(self, data: dict) -> None:
        try:
            self.applied_actions = set(data.get("applied_actions", []))
            self._splits = {s: {i: (date.fromisoformat(ex), Decimal(f)) for i, (ex, f) in v.items()}
                            for s, v in (data.get("splits") or {}).items()}
        except (AttributeError, TypeError, ValueError):
            return

    # ---- status ----------------------------------------------------------------------------------
    def status_info(self) -> dict:
        now = self.wall_clock()
        p = self.protocol
        rest_ok = self.rest.http.last_ok
        stream_quiet = p.phase == "ready" and p.last_message and now - p.last_message > 120
        rest_stale = rest_ok is None or now - rest_ok > 90
        open_now = cal.session(now) != "closed"
        stale = bool(open_now and (stream_quiet or (p.phase != "ready" and rest_stale)))
        info = FEEDS[self.data_feed]
        return {"realtime": info["realtime"], "data_source": "Alpaca", "data_exchange": info["exchange"],
                "data_feed": self.data_feed, "data_note": info["note"],
                "options_source": OPTION_FEEDS[self.options_feed],
                "stream": {"state": p.phase, "message": p.error, "streamed": len(p.streamed()), "cap": p.cap,
                           "polled": self._polled_count},
                "stale": stale, "rest_error": self.rest.http.last_error, "assets": len(self.info),
                "assets_error": self.assets_error, "corporate_actions": self.actions.status(), "fallback_reason": None}


# ---------------------------------------------------------------------------------------------
# Manual check: python -m app.marketdata.alpaca --check
# ---------------------------------------------------------------------------------------------
def _check(seconds: float = 8.0, thin: str = "IEP", transport: Optional[Transport] = None,
           connector=None, environ=None, out_dir: Optional[str] = None) -> int:
    import os
    import tempfile
    from .config import load_dotenv, project_root
    env = os.environ if environ is None else environ
    load_dotenv(project_root() / ".env", env)
    key, secret = env.get("APCA_API_KEY_ID", ""), env.get("APCA_API_SECRET_KEY", "")
    if not key or not secret:
        print("FAIL keys: APCA_API_KEY_ID and APCA_API_SECRET_KEY are not set (environment or .env)")
        return 1
    feed_name = env.get("PAPERDESK_ALPACA_FEED", "iex")
    feed = AlpacaFeed(key, secret, feed=feed_name, options_feed=env.get("PAPERDESK_ALPACA_OPTIONS_FEED", "indicative"),
                      trading_url=env.get("APCA_API_BASE_URL", PAPER_TRADING_URL), transport=transport,
                      connector=connector)
    results = []

    def item(name, fn):
        try:
            detail = fn()
            results.append(True)
            print(f"PASS {name}: {detail}")
        except Exception as e:
            results.append(False)
            msg = e.message if isinstance(e, ProviderError) else repr(e)
            print(f"FAIL {name}: {msg.replace(key, '<key>').replace(secret, '<secret>')}")

    def one_quote():
        feed.apply_snapshots(feed.rest.snapshots(["SPY"], feed.data_feed))
        q = feed.step(time.time())
        if not q:
            raise RuntimeError("no snapshot returned for SPY")
        return f"SPY bid {q[0].bid} ask {q[0].ask} last {q[0].last} ({feed.data_feed} feed)"

    def bar_set():
        rows = feed.bars("SPY", "1d", 30)
        if not rows:
            raise RuntimeError("no daily bars returned for SPY")
        return f"{len(rows)} daily bars, last close {rows[-1]['close']}"

    def chain():
        exps = feed.option_expirations("SPY")
        if not exps:
            raise RuntimeError("no SPY option expirations returned")
        ch = feed.option_chain("SPY", exps[min(1, len(exps) - 1)], time.time())
        if not ch:
            raise RuntimeError("no SPY option chain returned")
        return f"{exps[0]} first expiry, {len(ch['rows'])} strikes, {ch['model_legs']} legs fell back to the model"

    def stream():
        got = []
        feed.protocol.set_wanted(["SPY", "QQQ"])
        feed.stream.on_data = lambda msgs: got.extend(msgs)
        feed.stream.set_wanted(["SPY", "QQQ"])
        t = threading.Thread(target=feed.stream.run_once, daemon=True)
        t.start()
        t.join(seconds)
        feed.stream.stop()
        if feed.protocol.phase == "failed" or (feed.protocol.phase != "ready" and not got):
            raise RuntimeError(feed.protocol.error or f"stream state {feed.protocol.phase}")
        note = "" if got else " (no messages: the market may be closed)"
        return f"state {feed.protocol.phase}, {len(got)} messages in {seconds:.0f} s{note}"

    def actions():
        from .alpaca_actions import to_lifecycle
        from .research import corporate_action_records
        today = cal.ny_date(time.time())
        resp = feed.rest.corporate_actions("AAPL,MSFT,SPY,NVDA,TSLA", (today - timedelta(days=400)).isoformat(),
                                           (today + timedelta(days=90)).isoformat(), max_pages=20, strict=True)
        recs = corporate_action_records(resp)
        acts, notes = to_lifecycle(recs)
        kinds = sorted({r["type"] for r in recs})
        if not recs:
            raise RuntimeError("no corporate actions returned for AAPL, MSFT, SPY, NVDA and TSLA over 400 days")
        return (f"{len(recs)} records ({', '.join(kinds)}); {len(acts)} usable as dividends or splits, "
                f"{len(notes)} shown as notices")

    def listing():
        lst = feed.option_listing("SPY")
        if not lst:
            raise RuntimeError("no SPY option contracts listed")
        exps = sorted(lst)
        odd = [d for d in exps if d.weekday() != 4]
        n = sum(len(v) for v in lst.values())
        return (f"{len(exps)} SPY expiries from {exps[0]} to {exps[-1]}, {n} standard contracts' strikes, "
                f"{len(odd)} expiries not on a Friday (first {odd[0] if odd else 'none'})")

    def thin_symbol():
        snaps = feed.rest.snapshots([thin], feed.data_feed)
        if thin not in snaps:
            raise RuntimeError(f"no snapshot returned for {thin}")
        feed.apply_snapshots(snaps)
        q = (feed.step(time.time()) or [None])[0]
        note = feed.quote_note(thin)
        if q is None:
            raise RuntimeError(f"the {thin} snapshot gave no usable price")
        return (f"{thin} bid {q.bid} ask {q.ask} last {q.last} prev close {q.prev_close}"
                f"{'; ' + note['note'] if note else ''}")

    def replay_build():
        from .replay.format import read_session
        from .replay.provider import build_from_provider
        day = cal.ny_date(time.time()) - timedelta(days=1)
        while not cal.is_trading_day(day):
            day -= timedelta(days=1)
        d = out_dir or tempfile.mkdtemp(prefix="paperdesk-check-")
        stats = build_from_provider(feed.rest, day, ["SPY"], os.path.join(d, "check.pdsession.gz"))
        s = read_session(stats["path"])
        return (f"{stats['events']} events for SPY on {day} ({stats['feeds'].get('SPY', '?').upper()} bars), "
                f"source: {s.header.get('source')}; file {stats['path']}")

    item("assets", lambda: f"{feed.load_assets(max_age=0)} tradable symbols")
    item("quote", one_quote)
    item("bars", bar_set)
    item("option chain", chain)
    item("option contracts", listing)
    item("corporate actions", actions)
    item("thin symbol snapshot", thin_symbol)
    item("replay session from bars", replay_build)
    item("stream", stream)
    return 0 if all(results) else 1


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        thin = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--thin=")), "IEP")
        sys.exit(_check(thin=thin.upper()))
    print("usage: python -m app.marketdata.alpaca --check")
