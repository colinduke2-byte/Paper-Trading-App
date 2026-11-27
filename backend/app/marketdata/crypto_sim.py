"""Simulated crypto pairs for the synthetic feed: invented coins, never real ones.

Real coin names with made-up prices would mislead, so every coin here is fictional and its
name says so. Prices follow a seeded random walk that runs 24 hours a day, 7 days a week
(also when the simulated stock market is closed), with higher volatility than the stocks.
Prices use each pair's price increment, sizes its quantity increment.
"""
import math
import random
from collections import deque
from datetime import date, timedelta
from decimal import Decimal

from ..core.assets import crypto_info, round_to, spec_from_info
from ..core.models import Quote
from ..core.money import ZERO

# symbol, name, start price, annual volatility, quantity increment, price increment, minimum order, typical trade size
CRYPTO_UNIVERSE = [
    ("SIMCOIN/USD", "Simcoin (fictional coin)", "41250.00", 0.70, "0.00001", "0.01", "0.0001", "0.05"),
    ("SIMLEAF/USD", "Simleaf token (fictional coin)", "2180.50", 0.85, "0.0001", "0.01", "0.001", "0.8"),
    ("SIMBYTE/USD", "Simbyte (fictional coin)", "0.8421", 1.00, "0.01", "0.0001", "1", "2500"),
    ("SIMDUST/USD", "Simdust (fictional coin)", "0.004213", 1.30, "1", "0.000001", "100", "400000"),
]
CRYPTO_SYMBOLS = [c[0] for c in CRYPTO_UNIVERSE]
CRYPTO_SECONDS_PER_YEAR = 365 * 24 * 3600  # crypto never closes
_LOT = {c[0]: Decimal(c[7]) for c in CRYPTO_UNIVERSE}
_PRINT_SIZES = (Decimal("0.02"), Decimal("0.1"), Decimal("0.25"), Decimal("0.5"), Decimal(1), Decimal(1), Decimal(2), Decimal(5))


def crypto_infos() -> dict:
    return {sym: crypto_info(sym, name, qinc, pinc, mn, vol=vol, simulated=True)
            for sym, name, _, vol, qinc, pinc, mn, _ in CRYPTO_UNIVERSE}


def _utc_day(ts: float) -> date:
    return date(1970, 1, 1) + timedelta(days=int(ts // 86400))


def _size(rng: random.Random, sym: str, step: Decimal, scale: Decimal) -> Decimal:
    return max(round_to(_LOT[sym] * scale, step), step)


def make_quote(feed, sym: str, last: Decimal, now: float, prev_close, open_, high, low, volume, trade: bool,
               last_size=ZERO) -> Quote:
    """A quote around `last`: spread of a few basis points, at least one price increment."""
    spec = spec_from_info(sym, feed.info[sym])
    inc, qinc, rng = spec.price_increment, spec.qty_increment, feed.crypto_rng
    half = max(inc, round_to(last * Decimal("0.00005") * rng.randint(1, 3), inc))
    r = rng.random()
    if r < 0.4:
        bid, ask = last - 2 * half, last
    elif r < 0.8:
        bid, ask = last, last + 2 * half
    else:
        bid, ask = last - half, last + half
    bid = max(bid, inc)
    ask = max(ask, bid + inc)
    return Quote(symbol=sym, bid=bid, ask=ask, bid_size=_size(rng, sym, qinc, Decimal(rng.randint(1, 30)) / 5),
                 ask_size=_size(rng, sym, qinc, Decimal(rng.randint(1, 30)) / 5), last=last, last_size=last_size,
                 volume=volume, ts=now, trade=trade, open=open_, high=high, low=low, prev_close=prev_close)


def generate_history(feed, now: float, days: int) -> None:
    """Daily bars for every calendar day (crypto trades at weekends too) and today's minutes."""
    today = _utc_day(now)
    dates = [today - timedelta(days=i) for i in range(min(days, 730), 0, -1)]
    midnight = int(now // 86400) * 86400
    for sym, _, start, vol, _, pinc, _, _ in CRYPTO_UNIVERSE:
        rng = random.Random(f"{sym}-history")
        start_f = float(start)
        nd = max(0, -Decimal(pinc).normalize().as_tuple().exponent)
        daily_sigma = vol / math.sqrt(365)
        price, rows = start_f * rng.uniform(0.4, 0.9), []
        for d in dates:
            ret = daily_sigma * rng.gauss(0, 1) + 0.0003
            o = price * math.exp(daily_sigma * 0.2 * rng.gauss(0, 1))
            c = o * math.exp(ret)
            h = max(o, c) * (1 + abs(rng.gauss(0, daily_sigma * 0.5)))
            l = min(o, c) * (1 - abs(rng.gauss(0, daily_sigma * 0.5)))
            volume = rng.uniform(0.5, 1.8) * 5e7 / max(c, 1e-9) * (1 + 6 * abs(ret))
            rows.append([int((d - date(1970, 1, 1)).total_seconds()), o, h, l, c, volume])
            price = c
        scale = start_f / price  # anchor the walk so it ends at the listed price
        feed.store.daily[sym] = [[r[0], *(round(v * scale, nd) for v in r[1:5]), round(r[5] / scale, 4)] for r in rows]
        minute_sigma = vol * math.sqrt(60 / CRYPTO_SECONDS_PER_YEAR)
        p = start_f
        day_open, hi, lo, vol_sum = p, p, p, 0.0
        minutes = feed.store.minutes.setdefault(sym, deque(maxlen=6000))
        for t in range(midnight, int(now // 60) * 60, 60):
            o = p
            p = p * math.exp(minute_sigma * rng.gauss(0, 1))
            size = rng.uniform(0.2, 6) * float(_LOT[sym])
            minutes.append([t, round(o, nd), round(max(o, p) * 1.0003, nd), round(min(o, p) * 0.9997, nd), round(p, nd), size])
            hi, lo, vol_sum = max(hi, p), min(lo, p), vol_sum + size
        inc = Decimal(pinc)
        px = lambda v: max(round_to(Decimal(repr(v)), inc), inc)
        qinc = spec_from_info(sym, feed.info[sym]).qty_increment
        feed.quotes[sym] = make_quote(feed, sym, px(p), now, Decimal(start), px(day_open), px(hi), px(lo),
                                      round_to(Decimal(repr(vol_sum)), qinc), trade=False)
        feed._day[sym] = _utc_day(now)


def step(feed, sym: str, now: float, today: date, market: float) -> Quote:
    """Advance one coin to `now`. Rolls its day at UTC midnight."""
    info = feed.info[sym]
    spec = spec_from_info(sym, info)
    prev = feed.quotes[sym]
    dt = min(max(now - prev.ts, 0.0), 5.0) or 1.0
    if feed._day.get(sym) != today:
        day_ts = int((feed._day[sym] - date(1970, 1, 1)).total_seconds())
        feed.store.daily[sym].append([day_ts, float(prev.open), float(prev.high), float(prev.low), float(prev.last),
                                      float(prev.volume)])
        prev = Quote(**{**prev.__dict__, "prev_close": prev.last, "open": prev.last, "high": prev.last,
                        "low": prev.last, "volume": ZERO})
        feed._day[sym] = today
    rng = feed.crypto_rng
    sigma = info["vol"] * math.sqrt(dt / CRYPTO_SECONDS_PER_YEAR)
    shock = sigma * (0.3 * market + 0.95 * rng.gauss(0, 1))
    last = max(round_to(prev.last * Decimal(repr(math.exp(shock))), spec.price_increment), spec.price_increment)
    trade = rng.random() < 0.7
    size = _size(rng, sym, spec.qty_increment, rng.choice(_PRINT_SIZES)) if trade else ZERO
    if not trade:
        last = prev.last
    q = make_quote(feed, sym, last, now, prev.prev_close, prev.open, max(prev.high, last), min(prev.low, last),
                   prev.volume + size, trade, size)
    feed.quotes[sym] = q
    if trade:
        feed._record_print(q)
    return q
