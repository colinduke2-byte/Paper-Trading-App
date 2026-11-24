"""Synthetic market: fictional companies whose prices follow a seeded random walk.

This is the default feed so the program runs with no data subscription. Nothing it
produces is real market data; every company is invented.
"""
import math
import random
from collections import deque
from datetime import date, timedelta
from decimal import Decimal

from ..core import calendar as cal
from ..core.models import Quote
from ..core.money import CENT, ZERO, cents
from ..lifecycle.actions import dividend as dividend_action, split as split_action
from .feed import MarketDataProvider, adjust_for_split

# symbol, name, sector, start price, annual volatility, shares outstanding (millions), EPS, dividend per share
UNIVERSE = [
    ("NVLX", "Novalux Semiconductor", "Technology", 182.40, 0.48, 2450, 4.10, 0.16),
    ("ORBT", "Orbit Cloud Systems", "Technology", 311.75, 0.32, 7430, 11.20, 3.00),
    ("PXLR", "Pixelar Devices", "Technology", 204.10, 0.27, 15200, 6.45, 0.96),
    ("QNTM", "Quantum Ledger Software", "Technology", 88.30, 0.55, 410, -0.85, 0.0),
    ("SYNC", "Syncwave Networks", "Technology", 54.65, 0.30, 4050, 3.05, 1.60),
    ("HLTH", "Halcyon Therapeutics", "Health Care", 142.20, 0.38, 880, 5.20, 0.0),
    ("MEDX", "Meridian Medical Devices", "Health Care", 97.85, 0.22, 1310, 4.75, 2.20),
    ("BIOQ", "BioQuest Genomics", "Health Care", 23.40, 0.72, 260, -2.10, 0.0),
    ("FNBK", "First Northern Bancorp", "Financials", 61.10, 0.26, 2900, 5.60, 2.04),
    ("CAPX", "Capstone Exchange Group", "Financials", 228.90, 0.21, 570, 9.85, 4.40),
    ("INSR", "Harbor Mutual Insurance", "Financials", 119.35, 0.19, 640, 8.10, 3.10),
    ("ENRG", "Enerdyne Petroleum", "Energy", 108.60, 0.31, 4100, 9.30, 3.80),
    ("SOLR", "Solara Renewables", "Energy", 37.25, 0.58, 720, 0.45, 0.0),
    ("PIPE", "Continental Pipeline Partners", "Energy", 29.80, 0.24, 2200, 2.35, 2.10),
    ("RETL", "Redwood Retail Holdings", "Consumer", 163.45, 0.23, 2700, 6.90, 2.28),
    ("BREW", "Brewline Coffee Co", "Consumer", 92.15, 0.25, 1150, 3.55, 2.12),
    ("AUTO", "Voltaic Motors", "Consumer", 247.60, 0.62, 3180, 3.40, 0.0),
    ("SHOE", "Stride Athletic", "Consumer", 78.90, 0.29, 1500, 3.20, 1.36),
    ("AERO", "Aerodyne Aerospace", "Industrials", 196.30, 0.28, 610, 7.15, 2.60),
    ("RAIL", "Great Plains Railway", "Industrials", 236.75, 0.20, 610, 11.05, 5.20),
    ("BLDR", "Keystone Building Products", "Industrials", 67.40, 0.33, 300, 5.90, 0.80),
    ("UTIL", "Lakeshore Electric & Gas", "Utilities", 71.20, 0.17, 1090, 3.60, 2.72),
    ("TELE", "Crossband Communications", "Communication", 41.55, 0.22, 4200, 4.70, 2.66),
    ("STRM", "Streamhouse Media", "Communication", 512.30, 0.41, 430, 15.80, 0.0),
    ("MKTX", "Broad Market 500 Fund", "ETF", 452.10, 0.16, 900, 0.0, 6.10),
    ("TECX", "Technology 100 Fund", "ETF", 386.45, 0.22, 380, 0.0, 2.30),
]

TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
ALWAYS_OPEN_SECONDS_PER_YEAR = 252 * 24 * 3600
# Quarterly dividend months, by position in UNIVERSE modulo 3.
DIVIDEND_MONTHS = ((1, 4, 7, 10), (2, 5, 8, 11), (3, 6, 9, 12))
SPLIT_CANDIDATES = [u[0] for u in sorted(UNIVERSE, key=lambda u: -u[3])[:6]]  # the highest-priced listings


class SyntheticFeed(MarketDataProvider):
    name = "synthetic"
    simulated = True

    def __init__(self, seed: int = 7, now: float = 0.0, history_days: int = 520, always_open: bool = True):
        super().__init__()
        self.rng = random.Random(seed)
        self.always_open = always_open
        self.year_seconds = ALWAYS_OPEN_SECONDS_PER_YEAR if always_open else TRADING_SECONDS_PER_YEAR
        self._day: dict[str, date] = {}
        self._ex_drop: dict[str, Decimal] = {}  # dividend to take off the price at the next step
        for sym, name, sector, price, vol, shares, eps, div in UNIVERSE:
            self.info[sym] = {"symbol": sym, "name": name, "sector": sector, "vol": vol,
                              "shares_outstanding": shares * 1_000_000, "eps": eps, "dividend": div,
                              "type": "etf" if sector == "ETF" else "stock"}
        self._generate_history(now, history_days)

    # ---- history -------------------------------------------------------
    def _generate_history(self, now: float, days: int) -> None:
        today = self._trading_day(now)
        dates = []
        d = today - timedelta(days=1)
        while len(dates) < days:
            if d.weekday() < 5 and (self.always_open or cal.is_trading_day(d)):
                dates.append(d)
            d -= timedelta(days=1)
        dates.reverse()
        market = [self.rng.gauss(0.0003, 1) for _ in dates]
        for sym, _, _, start, vol, *_ in UNIVERSE:
            rng = random.Random(f"{sym}-history")
            daily_sigma = vol / math.sqrt(252)
            price, rows = start * rng.uniform(0.55, 0.9), []
            for d, mkt in zip(dates, market):
                ret = daily_sigma * (0.55 * mkt + 0.83 * rng.gauss(0, 1)) + 0.0004
                o = price * math.exp(daily_sigma * 0.3 * rng.gauss(0, 1))
                c = o * math.exp(ret)
                h = max(o, c) * (1 + abs(rng.gauss(0, daily_sigma * 0.5)))
                l = min(o, c) * (1 - abs(rng.gauss(0, daily_sigma * 0.5)))
                volume = int(rng.uniform(0.5, 1.8) * 4e8 / max(c, 1) * (1 + 8 * abs(ret)))
                ts = int((d - date(1970, 1, 1)).total_seconds())
                rows.append([ts, round(o, 2), round(h, 2), round(l, 2), round(c, 2), volume])
                price = c
            scale = start / price  # anchor the walk so it ends at the listed price
            rows = [[r[0], *(round(v * scale, 2) for v in r[1:5]), r[5]] for r in rows]
            price = start
            self.store.daily[sym] = rows
            prev_close = cents(Decimal(repr(round(price, 2))))
            # Intraday minutes for today so charts are not empty at start-up.
            midnight = int(cal.trading_day_start(today, self._mode))
            minute_sigma = vol * math.sqrt(60 / self.year_seconds)
            p = price * math.exp(daily_sigma * 0.3 * rng.gauss(0, 1))
            day_open = p
            hi, lo, vol_sum = p, p, 0
            for t in range(midnight, int(now // 60) * 60, 60):
                o = p
                p = p * math.exp(minute_sigma * rng.gauss(0, 1))
                size = int(rng.uniform(200, 4000))
                self.store.minutes.setdefault(sym, deque(maxlen=6000)).append(
                    [t, round(o, 2), round(max(o, p) * 1.0003, 2), round(min(o, p) * 0.9997, 2), round(p, 2), size])
                hi, lo, vol_sum = max(hi, p), min(lo, p), vol_sum + size
            last = cents(Decimal(repr(round(p, 2))))
            self.quotes[sym] = self._make_quote(sym, last, now, prev_close, cents(Decimal(repr(round(day_open, 2)))),
                                                cents(Decimal(repr(round(hi, 2)))), cents(Decimal(repr(round(lo, 2)))),
                                                Decimal(vol_sum), trade=False)
            self._day[sym] = self._trading_day(now)

    @property
    def _mode(self) -> str:
        return "always" if self.always_open else "us"

    def _trading_day(self, ts: float) -> date:
        """The day the daily bar and previous close belong to (core.calendar.trading_day)."""
        return cal.trading_day(ts, self._mode)

    def _make_quote(self, sym, last, now, prev_close, open_, high, low, volume, trade, last_size=ZERO) -> Quote:
        half = max(CENT, cents(last * Decimal("0.00015") * self.rng.randint(1, 3)))
        r = self.rng.random()
        if r < 0.4:
            bid, ask = last - 2 * half, last  # print at the ask
        elif r < 0.8:
            bid, ask = last, last + 2 * half  # print at the bid
        else:
            bid, ask = last - half, last + half
        bid = max(bid, CENT)
        ask = max(ask, bid + CENT)
        return Quote(symbol=sym, bid=bid, ask=ask, bid_size=Decimal(self.rng.randint(1, 30) * 100),
                     ask_size=Decimal(self.rng.randint(1, 30) * 100), last=last, last_size=last_size,
                     volume=volume, ts=now, trade=trade, open=open_, high=high, low=low, prev_close=prev_close)

    # ---- live stepping ---------------------------------------------------
    def step(self, now: float) -> list:
        if not self.always_open and cal.session(now) == "closed":
            return []
        out = []
        today = self._trading_day(now)
        market = self.rng.gauss(0, 1)
        for sym, info in self.info.items():
            prev = self.quotes[sym]
            dt = min(max(now - prev.ts, 0.0), 5.0) or 1.0
            if self._day.get(sym) != today:  # roll the day
                day_ts = int((self._day[sym] - date(1970, 1, 1)).total_seconds())
                self.store.daily[sym].append([day_ts, float(prev.open), float(prev.high), float(prev.low),
                                              float(prev.last), float(prev.volume)])
                prev = Quote(**{**prev.__dict__, "prev_close": prev.last, "open": prev.last, "high": prev.last,
                                "low": prev.last, "volume": ZERO})
                self._day[sym] = today
            if sym in self._ex_drop:  # the stock opens lower by the dividend on its ex-date
                drop = self._ex_drop.pop(sym)
                low = lambda v: max(v - drop, CENT)
                prev = Quote(**{**prev.__dict__, "last": low(prev.last), "open": low(prev.open), "low": low(prev.low)})
            sigma = info["vol"] * math.sqrt(dt / self.year_seconds)
            shock = sigma * (0.5 * market + 0.87 * self.rng.gauss(0, 1))
            last = cents(prev.last * Decimal(repr(math.exp(shock))))
            last = max(last, CENT)
            trade = self.rng.random() < 0.7
            size = Decimal(self.rng.choice([1, 5, 10, 25, 50, 100, 100, 100, 200, 300, 500, 1200])) if trade else ZERO
            if not trade:
                last = prev.last
            q = self._make_quote(sym, last, now, prev.prev_close, prev.open, max(prev.high, last), min(prev.low, last),
                                 prev.volume + size, trade, size)
            self.quotes[sym] = q
            if trade:
                self._record_print(q)
            out.append(q)
        return out

    # ---- corporate actions -------------------------------------------------
    def _business(self, d: date) -> date:
        while d.weekday() >= 5 or not (self.always_open or cal.is_trading_day(d)):
            d += timedelta(days=1)
        return d

    def corporate_actions(self, start: date, end: date) -> list:
        """Quarterly dividends from each symbol's annual `dividend` figure, on fixed dates, and one
        2-for-1 split a year (mid September) among the highest-priced listings."""
        out = []
        for y in range(start.year - 1, end.year + 1):
            for i, (sym, *_) in enumerate(UNIVERSE):
                amount = cents(Decimal(repr(float(self.info[sym].get("dividend", 0)))) / 4)
                if amount <= 0:
                    continue
                for month in DIVIDEND_MONTHS[i % 3]:
                    ex = self._business(date(y, month, 5 + (i * 7) % 20))
                    if start <= ex <= end:
                        out.append(dividend_action(sym, ex, self._business(ex + timedelta(days=14)), amount))
            ex = self._business(date(y, 9, 15))
            if start <= ex <= end:
                out.append(split_action(SPLIT_CANDIDATES[y % len(SPLIT_CANDIDATES)], ex, 2, 1))
        return out

    def _adjust_for_action(self, action) -> None:
        if action.kind == "split":
            adjust_for_split(self, action)
            info = self.info[action.symbol]
            info["dividend"] = round(float(info.get("dividend", 0)) * action.old / action.new, 6)
        elif action.kind == "dividend" and action.symbol in self.quotes:
            self._ex_drop[action.symbol] = self._ex_drop.get(action.symbol, ZERO) + action.amount

    # ---- persistence ------------------------------------------------------
    def snapshot(self) -> dict:
        return {"applied_actions": sorted(self.applied_actions),
                "dividends": {s: i.get("dividend", 0) for s, i in self.info.items()},
                "ex_drop": {s: str(v) for s, v in self._ex_drop.items()},
                "daily": self.store.daily,
                "minutes": {s: list(b) for s, b in self.store.minutes.items()},
                "days": {s: d.isoformat() for s, d in self._day.items()},
                "quotes": {s: {k: (str(v) if isinstance(v, Decimal) else v) for k, v in q.__dict__.items()}
                           for s, q in self.quotes.items()}}

    def restore(self, data: dict) -> None:
        try:
            quotes = {}
            for s, raw in data["quotes"].items():
                kw = {k: (Decimal(v) if k not in ("symbol", "ts", "trade") and v is not None else v) for k, v in raw.items()}
                kw["trade"] = False
                quotes[s] = Quote(**kw)
            if set(quotes) != set(self.info):
                return
            self.quotes = quotes
            self.store.daily = data["daily"]
            self.store.minutes = {s: deque(b, maxlen=6000) for s, b in data["minutes"].items()}
            self._day = {s: date.fromisoformat(d) for s, d in data["days"].items()}
            self.applied_actions = set(data.get("applied_actions", []))
            for s, div in data.get("dividends", {}).items():
                if s in self.info:
                    self.info[s]["dividend"] = div
            self._ex_drop = {s: Decimal(v) for s, v in data.get("ex_drop", {}).items()}
        except (KeyError, ValueError, TypeError):
            return
