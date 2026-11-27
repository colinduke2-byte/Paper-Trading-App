"""One definition of "the trading day": New York date in "us" mode, UTC date in "always" mode."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core import calendar as cal
from app.core.clock import SimClock
from app.exchange import Exchange
from app.lifecycle.days import Days
from app.marketdata.feed import SimFeed
from app.marketdata.synthetic import SyntheticFeed

D = Decimal


def utc(*a) -> float:
    return datetime(*a, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("before, after, day", [
    (utc(2026, 11, 24, 4, 59, 59), utc(2026, 11, 24, 5), date(2026, 11, 24)),   # EST: NY midnight is 05:00 UTC
    (utc(2026, 7, 1, 3, 59, 59), utc(2026, 7, 1, 4), date(2026, 7, 1)),         # EDT: 04:00 UTC
    (utc(2026, 3, 8, 4, 59, 59), utc(2026, 3, 8, 5), date(2026, 3, 8)),         # the night clocks go forward
    (utc(2026, 3, 9, 3, 59, 59), utc(2026, 3, 9, 4), date(2026, 3, 9)),         # first day on EDT
    (utc(2026, 11, 1, 3, 59, 59), utc(2026, 11, 1, 4), date(2026, 11, 1)),      # the night clocks go back
    (utc(2026, 11, 2, 4, 59, 59), utc(2026, 11, 2, 5), date(2026, 11, 2)),      # first day on EST
])
def test_us_trading_day_changes_at_new_york_midnight(before, after, day):
    assert cal.trading_day(before, "us") == day - timedelta(days=1)
    assert cal.trading_day(after, "us") == day
    assert cal.trading_day_start(day, "us") == after
    assert Days("us").today(after) == day and Days("us").today(before) == day - timedelta(days=1)


def test_always_trading_day_is_the_utc_date():
    t = utc(2026, 11, 24)
    assert cal.trading_day(t - 1, "always") == date(2026, 11, 23) and cal.trading_day(t, "always") == date(2026, 11, 24)
    assert cal.trading_day_start(date(2026, 11, 24), "always") == t
    assert Days("always").today(t) == date(2026, 11, 24)


def test_trading_day_start_round_trips_through_a_whole_year():
    for mode in ("us", "always"):
        d = date(2026, 1, 1)
        while d.year == 2026:
            start = cal.trading_day_start(d, mode)
            assert cal.trading_day(start, mode) == d and cal.trading_day(start - 1, mode) == d - timedelta(days=1)
            d += timedelta(days=1)


def _exchange(mode, t0):
    clock, feed = SimClock(t0), SimFeed()
    ex = Exchange(feed, clock, session_mode=mode)
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    a = ex.create_account("M", "margin", 10000).id
    ex.tick()
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    return ex, feed, clock, a


def _price(ex, feed, clock, t, px):
    clock.set(t)
    feed.set_quote("ABC", D(px) - D("0.01"), px, px)
    ex.tick()


def test_day_pnl_resets_at_new_york_midnight_in_us_mode():
    # Tuesday 2026-11-24, 15:00 New York (20:00 UTC)
    ex, feed, clock, a = _exchange("us", utc(2026, 11, 24, 20))
    _price(ex, feed, clock, utc(2026, 11, 24, 20, 30), "110.00")
    assert ex.summary(a)["day_pnl"] == D("100.00")
    _price(ex, feed, clock, utc(2026, 11, 25, 0, 30), "110.00")  # 19:30 New York: still the same day
    assert ex.summary(a)["day_pnl"] == D("100.00")
    _price(ex, feed, clock, utc(2026, 11, 25, 5, 0, 1), "110.00")  # just after midnight in New York
    assert ex.summary(a)["day_pnl"] == 0


def test_day_pnl_resets_at_utc_midnight_in_always_mode():
    ex, feed, clock, a = _exchange("always", utc(2026, 11, 24, 20))
    _price(ex, feed, clock, utc(2026, 11, 24, 23, 59), "110.00")
    assert ex.summary(a)["day_pnl"] == D("100.00")
    _price(ex, feed, clock, utc(2026, 11, 25, 0, 0, 1), "110.00")
    assert ex.summary(a)["day_pnl"] == 0


def test_day_orders_still_expire_at_utc_midnight_in_always_mode():
    ex, feed, clock, a = _exchange("always", utc(2026, 11, 24, 23, 59))
    o = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "limit", "limit_price": "50"})[0]
    _price(ex, feed, clock, utc(2026, 11, 25, 0, 0, 1), "100.00")
    assert o.status == "expired"


def test_synthetic_prev_close_rolls_with_the_new_york_day_in_us_mode():
    t0 = utc(2026, 11, 24, 20)  # 15:00 New York, regular session
    feed = SyntheticFeed(seed=3, now=t0, always_open=False)
    feed.step(t0 + 1)
    prev = feed.quotes["MKTX"].prev_close
    bars = len(feed.store.daily["MKTX"])
    feed.step(utc(2026, 11, 25, 0, 30))  # 19:30 New York (after hours), a new UTC date
    assert feed.quotes["MKTX"].prev_close == prev and len(feed.store.daily["MKTX"]) == bars
    feed.step(utc(2026, 11, 25, 9, 30))  # 04:30 New York, pre-market of the next day
    assert feed.quotes["MKTX"].prev_close != prev and len(feed.store.daily["MKTX"]) == bars + 1
    assert feed.store.daily["MKTX"][-1][0] == cal.trading_day_start(date(2026, 11, 24), "always")


def test_synthetic_feed_rolls_at_utc_midnight_in_always_mode():
    t0 = utc(2026, 11, 24, 23, 59)
    feed = SyntheticFeed(seed=3, now=t0, always_open=True)
    feed.step(t0 + 1)
    bars = len(feed.store.daily["MKTX"])
    feed.step(utc(2026, 11, 25, 0, 0, 5))
    assert len(feed.store.daily["MKTX"]) == bars + 1
