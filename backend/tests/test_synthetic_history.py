"""The synthetic feed's intraday history must not depend on the time of day it starts at."""
import pytest

from app.marketdata import synthetic
from app.marketdata.synthetic import PRIOR_MINUTE_DAYS, SyntheticFeed

DAY = 1_795_478_400  # 2026-11-25 00:00:00 UTC, a Wednesday
assert DAY % 86400 == 0
# Just after midnight, ten past, mid-morning and the last minute of the day.
STARTS = [DAY + 30, DAY + 600, DAY + 2 * 3600, DAY + 86400 - 20]


@pytest.mark.parametrize("now", STARTS)
def test_intraday_charts_have_enough_bars_at_any_time_of_day(now):
    feed = SyntheticFeed(seed=7, now=now)
    assert len(feed.bars("MKTX", "1m", 5000)) >= PRIOR_MINUTE_DAYS * 1440
    assert len(feed.bars("MKTX", "5m", 500)) == 500
    assert len(feed.bars("MKTX", "15m", 500)) >= PRIOR_MINUTE_DAYS * 96
    assert len(feed.bars("MKTX", "1h", 500)) >= PRIOR_MINUTE_DAYS * 24  # MACD 12 26 9 needs 34


@pytest.mark.parametrize("now", STARTS)
def test_prior_minutes_are_contiguous_well_formed_and_end_at_the_previous_close(now):
    feed = SyntheticFeed(seed=7, now=now)
    for sym in ("MKTX", "BIOQ", "STRM"):
        rows = list(feed.store.minutes[sym])
        assert rows[0][0] == DAY - PRIOR_MINUTE_DAYS * 86400
        assert [r[0] for r in rows] == list(range(rows[0][0], rows[0][0] + 60 * len(rows), 60))
        assert len(rows) < feed.store.max_minutes  # nothing was pushed out of the store
        prior = [r for r in rows if r[0] < DAY]
        assert len(prior) == PRIOR_MINUTE_DAYS * 1440
        for (_, o, h, l, c, v), nxt in zip(prior, prior[1:]):
            assert h >= max(o, c) and 0 < l <= min(o, c) and v > 0
            assert nxt[1] == c  # each bar opens where the one before closed
        assert prior[-1][4] == float(feed.quote(sym).prev_close)


@pytest.mark.parametrize("now", STARTS)
def test_prior_minutes_leave_daily_history_todays_bars_and_quotes_unchanged(now, monkeypatch):
    """The bundled replay samples are built from this feed and must stay byte-identical."""
    with_prior = SyntheticFeed(seed=7, now=now)
    monkeypatch.setattr(synthetic, "PRIOR_MINUTE_DAYS", 0)
    without = SyntheticFeed(seed=7, now=now)
    assert with_prior.store.daily == without.store.daily
    assert with_prior.quotes == without.quotes
    for sym in with_prior.info:
        today = [r for r in with_prior.store.minutes[sym] if r[0] >= DAY]
        assert today == list(without.store.minutes.get(sym, ()))
    assert [q.__dict__ for q in with_prior.step(now + 1)] == [q.__dict__ for q in without.step(now + 1)]
