"""Simulated economic calendar: a fictional economy with recurring releases on a fixed schedule.

Schedule (New York time), all on trading days:
- Jobs report: first Friday of the month, 8:30 am (moved to the next trading day on a holiday).
- Consumer prices: second Tuesday of the month, 8:30 am.
- Retail sales: the 16th (or the next trading day), 8:30 am.
- Economic growth (quarterly): last Thursday of January, April, July and October, 8:30 am.
- Central bank rate decision: last Wednesday of January, March, May, June, July, September, November
  and December, 2:00 pm.

Each series follows a seeded path that starts in January 2016, so a release always shows the same prior,
forecast and actual values. A release moves the whole simulated market by `market_move` (a log return,
scaled by each stock's beta), drawn from the seed and the size of the surprise.
"""
import math
from datetime import date, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache

from ...core import calendar as cal
from .profiles import business_on_or_after, ny_ts, rng

START_YEAR = 2016
RATE_MONTHS = (1, 3, 5, 6, 7, 9, 11, 12)
GDP_MONTHS = (1, 4, 7, 10)
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")

KINDS = {
    "jobs": ("Jobs report", "Employment"),
    "cpi": ("Consumer price index", "Inflation"),
    "retail": ("Retail sales", "Consumption"),
    "gdp": ("Economic growth (GDP, first estimate)", "Growth"),
    "rate": ("Central bank rate decision", "Monetary policy"),
}


def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    d = date(y, m, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(y: int, m: int, weekday: int) -> date:
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _previous_trading(d: date) -> date:
    while not cal.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def _dates(y: int, m: int) -> list:
    """(kind, release date, release time) for every release in month m of year y."""
    out = [("jobs", business_on_or_after(_nth_weekday(y, m, 4, 1)), time(8, 30)),
           ("cpi", business_on_or_after(_nth_weekday(y, m, 1, 2)), time(8, 30)),
           ("retail", business_on_or_after(date(y, m, 16)), time(8, 30))]
    if m in GDP_MONTHS:
        out.append(("gdp", _previous_trading(_last_weekday(y, m, 3)), time(8, 30)))
    if m in RATE_MONTHS:
        out.append(("rate", _previous_trading(_last_weekday(y, m, 2)), time(14, 0)))
    return out


def _r(x: float, places: str) -> Decimal:
    return Decimal(repr(x)).quantize(Decimal(places), ROUND_HALF_UP)


@lru_cache(maxsize=16)
def _series(seed: int, until_year: int) -> tuple:
    """Every release from START_YEAR through until_year, oldest first (cached per seed)."""
    out = []
    payrolls, unemployment, inflation, rate, growth = 180.0, 4.6, 1.6, 0.50, 2.2
    for y in range(START_YEAR, until_year + 1):
        for m in range(1, 13):
            r = rng(seed, "macro", y, m)
            # the economy this month
            prev = {"payrolls": payrolls, "unemployment": unemployment, "inflation": inflation, "rate": rate,
                    "growth": growth}
            payrolls = 170 + 0.55 * (payrolls - 170) + r.gauss(0, 70)
            unemployment = min(max(unemployment - 0.002 * (payrolls - 150) + r.gauss(0, 0.08), 3.2), 8.5)
            inflation = min(max(2.6 + 0.88 * (inflation - 2.6) + r.gauss(0, 0.22), -0.5), 8.0)
            retail = 0.35 + r.gauss(0, 0.55)
            for kind, d, t in sorted(_dates(y, m), key=lambda x: x[1]):
                er = rng(seed, "macro", kind, y, m)
                ref = MONTH_NAMES[(m - 2) % 12]  # monthly data describe the month before the release
                if kind == "jobs":
                    fc = 170 + 0.55 * (prev["payrolls"] - 170) + er.gauss(0, 25)
                    actual, forecast, previous, unit = payrolls, fc, prev["payrolls"], "thousand jobs"
                    z = (actual - forecast) / 70
                    extra = {"unemployment_rate": _r(unemployment, "0.1")}
                    values = (_r(actual, "1"), _r(forecast, "1"), _r(previous, "1"))
                elif kind == "cpi":
                    fc = 2.6 + 0.88 * (prev["inflation"] - 2.6) + er.gauss(0, 0.08)
                    actual, forecast, previous, unit = inflation, fc, prev["inflation"], "% a year"
                    z = -(actual - forecast) / 0.22  # hotter inflation than expected is bad for stocks
                    extra = {}
                    values = (_r(actual, "0.1"), _r(forecast, "0.1"), _r(previous, "0.1"))
                elif kind == "retail":
                    fc = 0.35 + er.gauss(0, 0.2)
                    actual, forecast, unit = retail, fc, "% from a month earlier"
                    z = (actual - forecast) / 0.55
                    extra = {}
                    values = (_r(actual, "0.1"), _r(forecast, "0.1"), None)
                elif kind == "gdp":
                    prev_g = growth
                    growth = 2.0 + 0.5 * (growth - 2.0) + 0.004 * (payrolls - 170) + er.gauss(0, 0.9)
                    fc = growth + er.gauss(0, 0.45)
                    actual, forecast, previous, unit = growth, fc, prev_g, "% annualised"
                    z = (actual - forecast) / 0.6
                    ref = f"Q{(m - 2) % 12 // 3 + 1}"
                    extra = {}
                    values = (_r(actual, "0.1"), _r(forecast, "0.1"), _r(previous, "0.1"))
                else:  # rate decision: lean against inflation away from 2.5%, sometimes surprise
                    gap = inflation - 2.5
                    want = 0.25 if gap > 0.6 and rate < 6 else -0.25 if gap < -0.4 and rate > 0.25 else 0.0
                    if er.random() < 0.15:
                        want += er.choice((-0.25, 0.25))
                    want = max(min(want, 0.5), -0.5)
                    expected = 0.25 if gap > 0.6 and rate < 6 else -0.25 if gap < -0.4 and rate > 0.25 else 0.0
                    previous_rate = rate
                    rate = max(rate + want, 0.0)
                    actual, forecast, previous, unit = rate, max(previous_rate + expected, 0.0), previous_rate, "%"
                    z = -(actual - forecast) / 0.12
                    ref = ""
                    extra = {"change": _r(rate - previous_rate, "0.01")}
                    values = (_r(actual, "0.01"), _r(forecast, "0.01"), _r(previous, "0.01"))
                sens = {"jobs": 0.0045, "cpi": 0.0055, "retail": 0.0025, "gdp": 0.003, "rate": 0.006}[kind]
                move = sens * max(min(0.8 * z, 3.0), -3.0) + er.gauss(0, sens * 0.5)
                name, category = KINDS[kind]
                out.append({"id": f"{kind}-{d.isoformat()}", "kind": kind, "name": name, "category": category,
                            "date": d.isoformat(), "time": t.strftime("%H:%M"), "ts": ny_ts(d, t),
                            "period": ref, "unit": unit, "actual": values[0], "forecast": values[1],
                            "previous": values[2], "surprise": z, "market_move": move, **extra})
    out.sort(key=lambda e: e["ts"])
    return tuple(out)


def releases(seed: int, start_ts: float, end_ts: float) -> list:
    """Scheduled releases with start_ts < ts <= end_ts (dicts; numbers are Decimal, model fields floats)."""
    y = cal.ny_date(end_ts).year
    if y < START_YEAR:
        return []
    return [e for e in _series(seed, y + 1) if start_ts < e["ts"] <= end_ts]


def public(e: dict, now: float, moves_applied: bool) -> dict:
    """One calendar row as shown to the user: no actual value before the release time."""
    released = e["ts"] <= now
    row = {k: e[k] for k in ("id", "kind", "name", "category", "date", "time", "ts", "period", "unit", "forecast",
                             "previous")}
    row.update(released=released, actual=e["actual"] if released else None, source="simulated",
               simulated=True, unemployment_rate=e.get("unemployment_rate") if released else None,
               change=e.get("change") if released else None,
               market_move_pct=(_r((math.exp(e["market_move"]) - 1) * 100, "0.01")
                                if released and moves_applied else None))
    if released and e["kind"] != "rate":
        row["beat"] = e["surprise"] > 0.15 if e["kind"] != "cpi" else None
    return row
