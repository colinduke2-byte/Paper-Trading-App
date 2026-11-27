"""Shared helpers: time conversion and account-derived series."""
import math
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from ..core.calendar import NY, utc_datetime
from ..core.money import ZERO, D
from ..ledger.ledger import CAPITAL

RATIO = Decimal("0.0000000001")  # ratios are reported to 10 decimal places


def ratio(x: Optional[Decimal]) -> Optional[Decimal]:
    return None if x is None else x.quantize(RATIO)


@lru_cache(maxsize=1 << 16)
def _ny_date_of_hour(hour: int) -> date:
    return utc_datetime(hour * 3600).astimezone(NY).date()


def ny_date(ts: float) -> date:
    # New York's UTC offset is always a whole number of hours, so the date is the same for a whole
    # UTC hour; caching by hour makes reports over many lots cheap.
    return _ny_date_of_hour(math.floor(ts / 3600))


def iso(ts: Optional[float]) -> str:
    """UTC ISO 8601 timestamp with millisecond precision ('' for none)."""
    if ts is None:
        return ""
    return utc_datetime(ts).isoformat(timespec="milliseconds")


def month_bounds(year: int, month: int) -> tuple[float, float]:
    """Start and end (exclusive) of a calendar month in New York time, as UNIX timestamps."""
    if not 1 <= month <= 12 or not 1970 <= year <= 9999:
        raise ValueError("month must be written as YYYY-MM")
    start = datetime(year, month, 1, tzinfo=NY)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=NY)
    return start.timestamp(), end.timestamp()


def parse_month(text: str) -> tuple[int, int]:
    try:
        y, m = text.split("-")
        if len(y) != 4 or len(m) != 2:
            raise ValueError
        year, month = int(y), int(m)
    except ValueError:
        raise ValueError("month must be written as YYYY-MM, for example 2026-11")
    month_bounds(year, month)
    return year, month


def months_between(start_ts: float, end_ts: float) -> list[str]:
    """Every YYYY-MM from the month containing start_ts to the one containing end_ts, newest first."""
    a, b = ny_date(start_ts), ny_date(end_ts)
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = y + (m == 12), m % 12 + 1
    return out[::-1]


def anniversary(d: date) -> date:
    """The same calendar day one year later (28 February for 29 February)."""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, day=28)


def equity_points(account, now: Optional[float] = None, current_equity: Optional[Decimal] = None) -> list:
    """Account value over time: the opening balance, every saved sample, then the live value."""
    pts = [(float(account.created_at), D(account.starting_cash))]
    for ts, eq in account.equity_history:
        ts = float(ts)
        if ts >= pts[-1][0]:
            if ts == pts[-1][0]:
                pts[-1] = (ts, D(eq))
            else:
                pts.append((ts, D(eq)))
    if now is not None and current_equity is not None and now >= pts[-1][0]:
        if now == pts[-1][0]:
            pts[-1] = (now, D(current_equity))
        else:
            pts.append((float(now), D(current_equity)))
    return pts


def external_flows(account) -> list:
    """Deposits (positive) and withdrawals (negative): every posting to contributed capital."""
    out = []
    for t in account.ledger.transactions:
        amt = -sum((v for a, v in t.postings if a == CAPITAL), ZERO)
        if amt:
            out.append((float(t.ts), amt))
    return out


def days(n: int) -> timedelta:
    return timedelta(days=n)
