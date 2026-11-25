"""US equity market calendar: holidays, early closes and session lookup."""
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
PRE_OPEN, OPEN, CLOSE, EARLY_CLOSE, POST_CLOSE = time(4), time(9, 30), time(16), time(13), time(20)


def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, m: int, weekday: int) -> date:
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(y, month, day)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def holidays(y: int) -> frozenset:
    out = set()
    ny = date(y, 1, 1)
    # New Year's Day on a Saturday is not observed on the preceding Friday.
    if ny.weekday() != 5:
        out.add(_observed(ny))
    out.add(_nth_weekday(y, 1, 0, 3))  # Martin Luther King Jr. Day
    out.add(_nth_weekday(y, 2, 0, 3))  # Presidents' Day
    out.add(easter(y) - timedelta(days=2))  # Good Friday
    out.add(_last_weekday(y, 5, 0))  # Memorial Day
    if y >= 2022:
        out.add(_observed(date(y, 6, 19)))  # Juneteenth
    out.add(_observed(date(y, 7, 4)))
    out.add(_nth_weekday(y, 9, 0, 1))  # Labor Day
    out.add(_nth_weekday(y, 11, 3, 4))  # Thanksgiving
    out.add(_observed(date(y, 12, 25)))
    return frozenset(out)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def is_early_close(d: date) -> bool:
    if not is_trading_day(d):
        return False
    if d == _nth_weekday(d.year, 11, 3, 4) + timedelta(days=1):
        return True
    return (d.month, d.day) in ((7, 3), (12, 24))


def close_time(d: date) -> time:
    return EARLY_CLOSE if is_early_close(d) else CLOSE


def ny_datetime(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(NY)


def ny_date(ts: float) -> date:
    return ny_datetime(ts).date()


def session(ts: float) -> str:
    """'pre', 'regular', 'post' or 'closed' for a UTC epoch timestamp."""
    dt = ny_datetime(ts)
    d, t = dt.date(), dt.time()
    if not is_trading_day(d):
        return "closed"
    close = close_time(d)
    if PRE_OPEN <= t < OPEN:
        return "pre"
    if OPEN <= t < close:
        return "regular"
    if close <= t < POST_CLOSE:
        return "post"
    return "closed"


def close_ts(d: date) -> float:
    """Epoch time of the regular-session close on date d."""
    return datetime.combine(d, close_time(d), tzinfo=NY).timestamp()


def previous_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


# ---- the trading day -------------------------------------------------------------------------
# One definition for everything that happens "once a day": day P&L, the lifecycle's business days,
# the synthetic feed's daily bars and previous close. Session mode "us" follows the New York date
# (so the day changes at midnight in New York, whatever the daylight-saving offset); "always" (the
# simulated market never closes) uses the UTC date. Day orders follow the sessions in "us" mode and
# the UTC day in "always" mode, which is the same boundary.
EPOCH = date(1970, 1, 1)


def utc_date(ts: float) -> date:
    return EPOCH + timedelta(days=int(ts // 86400))


def trading_day(ts: float, mode: str) -> date:
    """The trading day that timestamp ts belongs to in session mode `mode` ("us" or "always")."""
    return ny_date(ts) if mode == "us" else utc_date(ts)


def trading_day_start(d: date, mode: str) -> float:
    """Epoch time at which trading day d begins (New York or UTC midnight)."""
    return datetime.combine(d, time(0), tzinfo=NY if mode == "us" else timezone.utc).timestamp()
