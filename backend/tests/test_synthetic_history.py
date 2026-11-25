"""The synthetic feed has intraday history whatever the time of day it starts."""
import math
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.core import calendar as cal
from app.marketdata.synthetic import SyntheticFeed


def utc(*a) -> float:
    return datetime(*a, tzinfo=timezone.utc).timestamp()


def rsi(closes, period=14):
    """Wilder's RSI of the last bar, or None when there are too few bars (as the chart shows "–")."""
    if len(closes) < period + 1:
        return None
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])]
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        ag, al = (ag * (period - 1) + g) / period, (al * (period - 1) + l) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


# Wednesday 2026-11-25: 00:05 and 00:10 UTC are 19:05 and 19:10 New York on Tuesday (after hours);
# 13:35 UTC is 08:35 New York (pre-market). Sunday 2026-11-22 12:00 UTC: the US market is closed.
STARTS = [utc(2026, 11, 25, 0, 5), utc(2026, 11, 25, 0, 10), utc(2026, 11, 25, 13, 35), utc(2026, 11, 22, 12)]


@pytest.mark.parametrize("always_open", [True, False])
@pytest.mark.parametrize("now", STARTS)
def test_enough_five_minute_bars_for_rsi_14_at_any_start_time(now, always_open):
    feed = SyntheticFeed(seed=7, now=now, always_open=always_open)
    for sym in ("MKTX", "NVLX"):
        bars = feed.bars(sym, "5m", 500)
        assert len(bars) >= 200, (len(bars), always_open)
        value = rsi([b["close"] for b in bars])
        assert value is not None and 0 <= value <= 100 and not math.isnan(value)
        times = [b["time"] for b in bars]
        assert times == sorted(set(times)) and times[-1] < now
        assert all(b["low"] <= min(b["open"], b["close"]) and b["high"] >= max(b["open"], b["close"]) for b in bars)


@pytest.mark.parametrize("now", STARTS)
def test_us_mode_history_has_only_in_session_minutes(now):
    feed = SyntheticFeed(seed=7, now=now, always_open=False)
    rows = list(feed.store.minutes["MKTX"])
    assert len(rows) >= 1500
    assert all(cal.session(r[0]) != "closed" for r in rows)


@pytest.mark.parametrize("always_open", [True, False])
@pytest.mark.parametrize("now", STARTS)
def test_the_day_figures_follow_the_trading_day(now, always_open):
    mode = "always" if always_open else "us"
    feed = SyntheticFeed(seed=7, now=now, always_open=always_open)
    day_start = cal.trading_day_start(cal.trading_day(now, mode), mode)
    rows = list(feed.store.minutes["MKTX"])
    before, today = [r for r in rows if r[0] < day_start], [r for r in rows if r[0] >= day_start]
    q = feed.quotes["MKTX"]
    # minutes before today end exactly at the previous close, which is the last daily bar's close
    assert before and Decimal(str(before[-1][4])) == q.prev_close
    assert float(q.prev_close) == feed.store.daily["MKTX"][-1][4]
    if today:
        assert float(q.open) == today[0][1] and float(q.last) == today[-1][4]
        assert float(q.low) <= min(r[4] for r in today) and float(q.high) >= max(r[4] for r in today)
        assert q.volume == sum(r[5] for r in today)
    else:
        assert q.open == q.high == q.low == q.last and q.volume == 0


def test_always_mode_covers_the_previous_24_hours():
    now = utc(2026, 11, 25, 0, 5)
    rows = list(SyntheticFeed(seed=7, now=now).store.minutes["MKTX"])
    assert rows[0][0] <= now - 86400 and rows[-1][0] == now - 300 + 240


def test_history_is_reproducible():
    now = utc(2026, 11, 25, 13, 35)
    a, b = SyntheticFeed(seed=7, now=now), SyntheticFeed(seed=7, now=now)
    assert list(a.store.minutes["TECX"]) == list(b.store.minutes["TECX"]) and a.quotes["TECX"] == b.quotes["TECX"]
