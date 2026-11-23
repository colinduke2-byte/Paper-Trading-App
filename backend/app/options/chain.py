"""Simulated option chain: listed expiries and strikes, a volatility surface and quotes.

All option prices here are MODEL prices derived from the underlying quote. They are
not market data and are labelled as simulated everywhere they are shown.
"""
import hashlib
import math
from datetime import date, timedelta
from decimal import Decimal

from ..core import calendar as cal
from ..core.models import Quote
from ..core.money import CENT, ZERO, ceil_cent, floor_cent, model_to_price
from . import pricing
from .symbology import Contract, build

RISK_FREE = 0.04
YEAR_SECONDS = 365 * 24 * 3600


def _friday_expiry(d: date) -> date:
    """The Friday of d's week, moved back to the previous trading day on a holiday."""
    d += timedelta(days=(4 - d.weekday()) % 7)
    while not cal.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def expirations(today: date, weekly: int = 5, monthly: int = 4) -> list:
    out = set()
    d = today
    while len(out) < weekly:
        e = _friday_expiry(d)
        if e >= today:
            out.add(e)
        d += timedelta(days=7)
    y, m, added = today.year, today.month, 0
    while added < monthly:
        first = date(y, m, 1)
        third_friday = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
        e = _friday_expiry(third_friday)
        if e >= today and e not in out:
            out.add(e)
            added += 1
        elif e in out:
            pass
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if (y - today.year) > 3:
            break
    return sorted(out)


def strike_step(spot: float) -> Decimal:
    if spot < 25:
        return Decimal("1")
    if spot < 100:
        return Decimal("2.5")
    if spot < 250:
        return Decimal("5")
    return Decimal("10")


def strikes(spot: float, width: float = 0.25) -> list:
    step = strike_step(spot)
    lo = (Decimal(str(spot * (1 - width))) // step) * step
    hi = (Decimal(str(spot * (1 + width))) // step + 1) * step
    out, k = [], max(lo, step)
    while k <= hi:
        out.append(k)
        k += step
    return out


def surface_vol(base_vol: float, S: float, K: float, T: float) -> float:
    """Base volatility with a skew (puts richer) and a smile that flattens with time."""
    m = math.log(K / S)
    term = 1 + 0.15 * math.exp(-T * 8)
    v = base_vol * term * (1 - 0.35 * m + 1.2 * m * m / max(math.sqrt(T * 4), 0.25))
    return min(max(v, 0.05), 3.0)


def _stable(symbol: str, salt: str, n: int) -> int:
    return int(hashlib.sha1(f"{symbol}:{salt}".encode()).hexdigest()[:8], 16) % n


def option_snapshot(c: Contract, S: float, base_vol: float, now: float) -> dict:
    """Model price, quote and greeks for one contract."""
    T = max(c.expiry_ts - now, 0.0) / YEAR_SECONDS
    K = float(c.strike)
    iv = surface_vol(base_vol, S, K, max(T, 1e-6))
    fair = pricing.price(S, K, T, RISK_FREE, iv, c.kind)
    g = pricing.greeks(S, K, T, RISK_FREE, iv, c.kind)
    half = max(0.01, 0.012 * fair) + 0.005
    bid = max(floor_cent(Decimal(repr(fair - half))), ZERO)
    ask = max(ceil_cent(Decimal(repr(fair + half))), bid + CENT)
    mid = model_to_price(fair)
    moneyness = abs(math.log(K / S))
    liquidity = math.exp(-moneyness * 12)
    oi = int((200 + _stable(c.symbol, "oi", 9000)) * liquidity) + _stable(c.symbol, "oi2", 40)
    vol = int((20 + _stable(c.symbol, "vol", 2500)) * liquidity)
    size = Decimal(5 + _stable(c.symbol, "sz", 60))
    return {"symbol": c.symbol, "underlying": c.underlying, "expiry": c.expiry.isoformat(), "kind": c.kind,
            "strike": c.strike, "bid": bid, "ask": ask, "mid": mid, "last": mid, "bid_size": size,
            "ask_size": size, "iv": iv, "open_interest": oi, "volume": vol, "intrinsic": pricing.intrinsic(S, K, c.kind),
            "dte": max((c.expiry - cal.ny_date(now)).days, 0), **g}


def option_quote(c: Contract, S: float, base_vol: float, now: float) -> Quote:
    s = option_snapshot(c, S, base_vol, now)
    return Quote(symbol=c.symbol, bid=s["bid"], ask=s["ask"], bid_size=s["bid_size"], ask_size=s["ask_size"],
                 last=max(s["mid"], CENT), ts=now)


def chain(underlying: str, expiry: date, S: float, base_vol: float, now: float) -> list:
    rows = []
    for k in strikes(S):
        row = {"strike": k}
        for kind, key in (("C", "call"), ("P", "put")):
            c = Contract(build(underlying, expiry, kind, k), underlying, expiry, kind, k)
            row[key] = option_snapshot(c, S, base_vol, now)
        rows.append(row)
    return rows
