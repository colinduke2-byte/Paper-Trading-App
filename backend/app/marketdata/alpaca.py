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
from dataclasses import dataclass, field
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
from .feed import TIMEFRAMES, MarketDataProvider
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

    def corporate_actions(self, symbols: str, start: str, end: str, types: Optional[str] = None, **kw) -> dict:
        out, token = {}, None
        for _ in range(5):
            j = self._data("/v1/corporate-actions", {"symbols": symbols, "start": start, "end": end, "types": types,
                                                     "limit": 1000, "page_token": token}, **kw) or {}
            for k, rows in (j.get("corporate_actions") or {}).items():
                out.setdefault(k, []).extend(rows or [])
            token = j.get("next_page_token")
            if not token:
                break
        return out


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
            self.prev_close = dec(prev.get("c"))
        return changed

    def to_quote(self) -> Optional[Quote]:
        """A usable Quote, or None when nothing is known yet.

        A missing or crossed side (common outside regular hours on IEX) is filled from the
        last trade so the quote is never zero-priced; see docs/DATA_PROVIDERS.md.
        """
        bid = self.bid if self.bid and self.bid > 0 else None
        ask = self.ask if self.ask and self.ask > 0 else None
        last = self.last
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
        return Quote(symbol=self.symbol, bid=bid, ask=ask, bid_size=self.bid_size, ask_size=self.ask_size,
                     last=last, last_size=self.last_size if self.new_trade else ZERO, volume=self.volume,
                     ts=self.ts, trade=self.new_trade, open=self.open, high=self.high, low=self.low,
                     prev_close=self.prev_close)


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
                source: str, open_interest: Optional[dict] = None) -> list:
    by_strike: dict = {}
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
        # Entered as a context manager: websockets 17.1+ deprecates using connect()'s return value directly
        # (it warns once the handshake is done); older versions return the connection, whose __enter__ is
        # itself, so this works with every supported version.
        self._cm = connect(url, open_timeout=15, close_timeout=3, max_size=2 ** 24)
        self._ws = self._cm.__enter__()

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
            self._cm.__exit__(None, None, None)
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
        with self._lock:
            changed, self._changed = self._changed, set()
            out = []
            for sym in changed:
                st = self._state.get(sym)
                q = st.to_quote() if st else None
                if q is None:
                    continue
                st.new_trade = False
                self.quotes[sym] = q
                out.append(q)
        return out

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
                st = self._state.setdefault(sym, SymbolState(sym))
                if m.get("T") == "q":
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

    def apply_snapshots(self, snaps: dict) -> None:
        now = self.wall_clock()
        today = cal.ny_date(now)
        with self._lock:
            for sym, snap in (snaps or {}).items():
                if not snap:
                    continue
                st = self._state.setdefault(sym, SymbolState(sym))
                if st.apply_snapshot(snap, today):
                    st.received = now
                    self._changed.add(sym)

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
        return parse_bars(self.rest.bars(symbol, TF_ALPACA[tf], start, None, n, self.data_feed,
                                         "split" if daily else "raw", max_wait=8), daily)

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
        now = self.wall_clock()
        hit = self._exp_cache.get(underlying)
        if hit and now - hit[0] < 3600:
            return hit[1]
        today = cal.ny_date(now)
        try:
            rows = self.rest.option_contracts(underlying, today.isoformat(), (today + timedelta(days=800)).isoformat(),
                                              max_wait=8)
        except ProviderError as e:
            log.warning("Alpaca option contracts %s: %s", underlying, e.message)
            return hit[1] if hit else None
        exps = sorted({date.fromisoformat(r["expiration_date"]) for r in rows if r.get("expiration_date")})
        for r in rows:
            if r.get("open_interest") not in (None, ""):
                self._oi[r["symbol"]] = r["open_interest"]
        if not exps:
            return None
        self._exp_cache[underlying] = (now, exps)
        return exps

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
        try:
            snaps = self.rest.option_chain(underlying, self.options_feed, expiry.isoformat(),
                                           strike_gte=f"{spot * 0.7:.2f}", strike_lte=f"{spot * 1.3:.2f}", max_wait=8)
        except ProviderError as e:
            log.warning("Alpaca option chain %s: %s", underlying, e.message)
            return hit[1] if hit else None
        if not snaps:
            return None
        self._store_option_snaps(snaps)
        rows = build_chain(underlying, expiry, snaps, spot, self.base_vol(underlying), now, self.options_feed, self._oi)
        if not rows:
            return None
        model_legs = sum(1 for r in rows for k in ("call", "put") if r[k]["source"] == "model")
        out = {"underlying": underlying, "expiry": expiry.isoformat(), "spot": spot, "simulated": False,
               "source": f"alpaca-{self.options_feed}", "source_label": OPTION_FEEDS[self.options_feed],
               "as_of": wall, "model_legs": model_legs, "rows": rows}
        self._chain_cache[key] = (wall, out)
        return out

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
                "assets_error": self.assets_error, "fallback_reason": None}


# ---------------------------------------------------------------------------------------------
# Manual check: python -m app.marketdata.alpaca --check
# ---------------------------------------------------------------------------------------------
def _check(seconds: float = 8.0, environ=None, out=None, **feed_kw) -> int:
    """One line per check (PASS or FAIL with the reason), then a summary. Exit code 0 only if all pass.

    The adapter's own log messages are captured and folded into the FAIL line they explain, so nothing
    else is printed. feed_kw (transport, connector) lets tests run it against saved responses."""
    import logging
    import os
    import warnings
    from ..core import envfile
    env = os.environ if environ is None else environ
    envfile.load_into(envfile.env_file_path(env), env)
    say = out or (lambda line: print(line, flush=True))
    key, secret = env.get("APCA_API_KEY_ID", ""), env.get("APCA_API_SECRET_KEY", "")
    if not key or not secret:
        say("FAIL keys: APCA_API_KEY_ID and APCA_API_SECRET_KEY are not set (environment or .env)")
        say("SUMMARY: 0 of 1 checks passed")
        return 1

    class Collect(logging.Handler):
        def __init__(self):
            super().__init__(logging.WARNING)
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    collect = Collect()
    loggers = [logging.getLogger(n) for n in ("paperdesk", "websockets")]
    saved = [(lg, lg.handlers[:], lg.propagate, lg.level) for lg in loggers]
    for lg in loggers:
        lg.handlers, lg.propagate = [collect], False
        lg.setLevel(logging.WARNING)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # library warnings are not check results
            return _run_checks(seconds, env, key, secret, say, collect, feed_kw)
    finally:
        for lg, handlers, propagate, level in saved:
            lg.handlers, lg.propagate = handlers, propagate
            lg.setLevel(level)


def _run_checks(seconds, env, key, secret, say, collect, feed_kw) -> int:
    try:
        feed = AlpacaFeed(key, secret, feed=env.get("PAPERDESK_ALPACA_FEED", "iex").strip().lower(),
                          options_feed=env.get("PAPERDESK_ALPACA_OPTIONS_FEED", "indicative").strip().lower(),
                          trading_url=env.get("APCA_API_BASE_URL", PAPER_TRADING_URL).strip().rstrip("/"), **feed_kw)
    except ValueError as e:
        say(f"FAIL settings: {e}")
        say("SUMMARY: 0 of 1 checks passed")
        return 1
    results = []

    def item(name, fn):
        collect.messages.clear()
        try:
            detail = fn()
            results.append(True)
            say(f"PASS {name}: {detail}")
        except Exception as e:
            results.append(False)
            msg = e.message if isinstance(e, ProviderError) else str(e) or type(e).__name__
            why = "; ".join(dict.fromkeys(m for m in collect.messages if m not in msg))
            say(f"FAIL {name}: {msg}" + (f" ({why})" if why else ""))

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

    item("assets", lambda: f"{feed.load_assets(max_age=0)} tradable symbols")
    item("quote", one_quote)
    item("bars", bar_set)
    item("option chain", chain)
    item("stream", stream)
    say(f"SUMMARY: {sum(results)} of {len(results)} checks passed" + ("" if all(results) else
        "; see the FAIL lines above and docs/DATA_PROVIDERS.md"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        sys.exit(_check())
    print("usage: python -m app.marketdata.alpaca --check")
