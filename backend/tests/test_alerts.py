"""Alert engine and notification centre (app/alerts, app/api/routes_alerts.py)."""
import json
import os
import shutil
import time
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

from app.alerts import indicators, model
from app.alerts.engine import migrate
from app.alerts.notifications import CAP
from app.api.server import create_app
from app.core.clock import SimClock
from app.exchange import Exchange
from app.lifecycle.actions import dividend, split
from app.marketdata.feed import SimFeed
from app.marketdata.synthetic import SyntheticFeed
from app.options import chain
from app.options.symbology import build
from helpers import D, T0, make_exchange, move

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
EX = date(2026, 11, 25)


def fired(ex, kind="alert"):
    return [e for e in ex.drain_events() if e["type"] == kind]


def notes(ex, aid, category=None):
    return ex.alerting.notices.list(aid, category=category, limit=500)


def setup(**prices):
    ex, feed, clock = make_exchange(**prices)
    a = ex.create_account("Margin", "margin").id
    ex.drain_events()
    return ex, feed, clock, a


# ---- price ---------------------------------------------------------------------------------------
def test_price_alert_fires_exactly_once_and_notifies_every_account():
    ex, feed, clock, a = setup()
    b = ex.create_account("Cash", "cash", D("25000")).id
    al = ex.alerting.create({"kind": "price", "symbol": "ABC", "op": "above", "price": "105"})
    move(ex, feed, clock, "ABC", "104.99")
    assert fired(ex) == [] and al["last_value"] == "104.99"
    move(ex, feed, clock, "ABC", "105")
    move(ex, feed, clock, "ABC", "107")
    move(ex, feed, clock, "ABC", "100")
    move(ex, feed, clock, "ABC", "106")
    assert al["status"] == "triggered" and al["fire_count"] == 1 and al["triggered_at"] == T0 + 2
    for x in (a, b):
        [n] = notes(ex, x, "alert")
        assert n["text"] == "ABC rises to 105.00: now 105.00." and n["symbol"] == "ABC" and not n["read"]


def test_price_below_and_account_scoped_alert_reaches_only_that_account():
    ex, feed, clock, a = setup()
    b = ex.create_account("Other", "margin").id
    ex.alerting.create({"kind": "price", "symbol": "ABC", "op": "below", "price": "95", "account_id": b})
    move(ex, feed, clock, "ABC", "94.50")
    assert notes(ex, a, "alert") == [] and len(notes(ex, b, "alert")) == 1
    [e] = fired(ex)
    assert e["account_id"] == b


def test_rearming_alert_waits_for_reset_and_cooldown():
    ex, feed, clock, a = setup()
    al = ex.alerting.create({"kind": "price", "symbol": "ABC", "op": "above", "price": "105", "repeat": True,
                             "cooldown": 60})
    move(ex, feed, clock, "ABC", "106")  # t=1: fires
    assert al["fire_count"] == 1
    for _ in range(5):
        move(ex, feed, clock, "ABC", "107")  # still above: no reset
    assert al["fire_count"] == 1
    move(ex, feed, clock, "ABC", "104")  # reset seen
    move(ex, feed, clock, "ABC", "106")  # inside the cool-down
    assert al["fire_count"] == 1 and al["status"] == "active"
    move(ex, feed, clock, "ABC", "106", seconds=60)  # cool-down over, condition still true: fires again
    assert al["fire_count"] == 2
    move(ex, feed, clock, "ABC", "106", seconds=600)  # no reset since: stays quiet
    assert al["fire_count"] == 2
    assert len(notes(ex, a, "alert")) == 2


# ---- percent change and volume ------------------------------------------------------------------
def test_percent_change_on_the_day_and_from_a_reference():
    ex, feed, clock, a = setup()  # previous close of ABC is 100 in the scripted feed
    day = ex.alerting.create({"kind": "change", "symbol": "ABC", "op": "below", "value": "-3", "basis": "day"})
    ref = ex.alerting.create({"kind": "change", "symbol": "ABC", "op": "above", "value": "10", "basis": "reference",
                              "reference": "90"})
    move(ex, feed, clock, "ABC", "98")
    assert day["status"] == "active" and day["last_value"] == "-2.00%" and ref["last_value"] == "8.89%"
    move(ex, feed, clock, "ABC", "97")
    assert day["status"] == "triggered"
    move(ex, feed, clock, "ABC", "99")
    assert ref["status"] == "triggered"
    texts = [n["text"] for n in notes(ex, a, "alert")]
    assert "ABC change on the day falls to -3.00%: now -3.00%." in texts
    assert "ABC change from 90.00 rises to 10.00%: now 10.00%." in texts


def test_change_from_reference_defaults_to_the_current_price():
    ex, feed, clock, a = setup()
    al = ex.alerting.create({"kind": "change", "symbol": "ABC", "op": "above", "value": "5", "basis": "reference"})
    assert al["params"]["reference"] == "100.00"


def test_volume_level_and_multiple_of_average():
    ex, feed, clock, a = setup()
    day = T0 // 86400 * 86400
    feed.store.daily["ABC"] = [[day - 86400 * i, 100, 100, 100, 100, 1000.0] for i in range(30, 0, -1)]
    lvl = ex.alerting.create({"kind": "volume", "symbol": "ABC", "value": "500", "mode": "level"})
    avg = ex.alerting.create({"kind": "volume", "symbol": "ABC", "value": "2", "mode": "average"})
    # volume so far today: 100 (from make_exchange); each move adds a 100-share print
    for _ in range(3):
        move(ex, feed, clock, "ABC", "100")
    assert lvl["status"] == "active"
    move(ex, feed, clock, "ABC", "100")  # 500
    assert lvl["status"] == "triggered" and avg["status"] == "active"
    for _ in range(15):
        move(ex, feed, clock, "ABC", "100")  # 2000 = 2 x the 1000-share average
    assert avg["status"] == "triggered"


# ---- indicators ------------------------------------------------------------------------------------
@settings(max_examples=60, deadline=None)
@given(st.lists(st.floats(1, 1000, allow_nan=False), min_size=1, max_size=80), st.integers(2, 20))
def test_incremental_indicators_match_the_batch_definitions(xs, n):
    sma, ema, rsi = indicators.SMA(n), indicators.EMA(n), indicators.RSI(n)
    for i, x in enumerate(xs):
        for c in (sma, ema, rsi):
            c.push(x)
        seen = xs[:i + 1]
        for got, want in ((sma.value, indicators.sma_batch(seen, n)), (ema.value, indicators.ema_batch(seen, n)),
                          (rsi.value, indicators.rsi_batch(seen, n))):
            assert (got is None) == (want is None)
            if got is not None:
                assert got == pytest.approx(want, rel=1e-9, abs=1e-6)


def test_rsi_reference_value():
    # Wilder's example series (14 periods): the first RSI is about 70.46 (New Concepts in Technical Trading Systems).
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    r = indicators.RSI(14)
    for c in closes:
        r.push(c)
    assert r.value == pytest.approx(70.46, abs=0.05)


def bars_minute(ex, feed, clock, prices):
    """One print per minute: each price closes one 1-minute bar."""
    for p in prices:
        clock.set((clock.now() // 60 + 1) * 60 + 1)
        feed.set_quote("ABC", D(str(p)) - D("0.01"), D(str(p)), D(str(p)))
        ex.tick()


def test_price_crossing_its_moving_average_fires_on_the_bar_close():
    ex, feed, clock, a = setup()
    bars_minute(ex, feed, clock, [100] * 6)
    al = ex.alerting.create({"kind": "indicator", "symbol": "ABC", "indicator": "sma", "period": 5, "tf": "1m",
                             "op": "crosses_above"})
    assert ex.alerting._series[("ABC", "1m", "sma", 5)].calc.value == pytest.approx(100)
    bars_minute(ex, feed, clock, [99, 99])  # below the average: no cross
    assert al["status"] == "active"
    bars_minute(ex, feed, clock, [102])  # the 102 bar is still forming
    assert al["status"] == "active"
    bars_minute(ex, feed, clock, [102])  # the 102 bar closed above the SMA (99.8 -> 100.4): fired
    assert al["status"] == "triggered" and al["fire_count"] == 1
    [n] = notes(ex, a, "alert")
    assert n["text"].startswith("ABC closes above its 5-bar SMA (1-minute bars, crossing): now ")


class LateHistoryFeed(SimFeed):
    """A feed that, like a live one, has no history in memory until its loader has fetched it.
    `deliver()` is the loader: it copies the bars asked for, as a provider request made now would."""

    def __init__(self):
        super().__init__()
        self.loads, self.wanted, self.ready, self.asked_directly = 0, set(), {}, 0

    def bars(self, symbol, tf, limit=500):
        self.asked_directly += 1  # the alert engine must never do this
        return super().bars(symbol, tf, limit)

    def history_mark(self):
        return self.loads

    def bars_ready(self, symbol, tf, limit=500, after=None):
        hit = self.ready.get((symbol, tf))
        if hit and (after is None or hit[0] > after):
            return hit[1][-limit:]
        self.wanted.add((symbol, tf))
        return None

    def deliver(self):
        for key in sorted(self.wanted):
            self.loads += 1
            self.ready[key] = (self.loads, SimFeed.bars(self, *key, 5000))
        self.wanted = set()


def late_setup():
    clock, feed = SimClock(T0), LateHistoryFeed()
    ex = Exchange(feed, clock)
    feed.set_quote("ABC", D("99.99"), D("100"), D("100"))
    ex.tick()
    return ex, feed, clock, ex.create_account("Margin", "margin").id


def test_an_indicator_alert_waits_for_history_and_is_evaluated_once_it_arrives():
    ex, feed, clock, a = late_setup()
    bars_minute(ex, feed, clock, [100] * 6)
    al = ex.alerting.create({"kind": "indicator", "symbol": "ABC", "indicator": "sma", "period": 5, "tf": "1m",
                             "op": "crosses_above"})
    s = ex.alerting._series[("ABC", "1m", "sma", 5)]
    assert not s.seeded and al["last_value"] == "waiting for price history"
    bars_minute(ex, feed, clock, [99])  # no history yet: nothing is evaluated, nothing is fetched
    assert not s.seeded and al["last_value"] == "waiting for price history" and ex.alerting.evaluations == 0
    feed.deliver()
    move(ex, feed, clock, "ABC", "99")  # seeded from memory on the next tick
    assert s.seeded and s.calc.value == pytest.approx(100)
    bars_minute(ex, feed, clock, [99])  # the first 99 bar closed, but nothing read since then is in memory
    assert s.waiting and s.close == pytest.approx(100)
    move(ex, feed, clock, "ABC", "99")
    assert s.waiting and s.close == pytest.approx(100)
    feed.deliver()
    move(ex, feed, clock, "ABC", "99")  # read on the tick after the load
    assert not s.waiting and s.close == pytest.approx(99) and al["status"] == "active"
    for price in (102, 102):  # the second 99 bar closes, then the first 102 bar: above the average
        assert al["status"] == "active"
        bars_minute(ex, feed, clock, [price])
        feed.deliver()
        move(ex, feed, clock, "ABC", price)
    assert al["status"] == "triggered" and al["fire_count"] == 1
    assert feed.asked_directly == 0


def test_average_volume_alert_waits_for_history():
    ex, feed, clock, a = late_setup()
    day = T0 // 86400 * 86400
    feed.store.daily["ABC"] = [[day - 86400 * i, 100, 100, 100, 100, 1000.0] for i in range(30, 0, -1)]
    avg = ex.alerting.create({"kind": "volume", "symbol": "ABC", "value": "2", "mode": "average"})
    for _ in range(25):
        move(ex, feed, clock, "ABC", "100")  # 2600 shares: over twice the average, but the average is unknown
    assert avg["status"] == "active" and avg["last_value"] == "no volume history yet"
    feed.deliver()
    move(ex, feed, clock, "ABC", "100")
    assert avg["status"] == "triggered" and feed.asked_directly == 0


@settings(max_examples=40, deadline=None)
@given(st.lists(st.tuples(st.integers(90, 110), st.booleans()), min_size=1, max_size=40))
def test_late_history_loses_no_bar_and_counts_none_twice(steps):
    """Whenever the loads arrive, once the history is in memory the series holds exactly what a feed with
    its bars in memory gives: every closed bar pushed once, in order (a long gap is rebuilt from history)."""
    late, lfeed, lclock, _ = late_setup()
    ex, feed, clock, _ = setup()
    req = {"kind": "indicator", "symbol": "ABC", "indicator": "sma", "period": 3, "tf": "1m", "op": "crosses_above",
           "repeat": True}
    late.alerting.create(req)
    ex.alerting.create(req)
    for price, loaded in steps:
        bars_minute(ex, feed, clock, [price])
        bars_minute(late, lfeed, lclock, [price])
        if loaded:
            lfeed.deliver()
    for _ in range(2):  # at most a seed and one catch-up remain
        move(late, lfeed, lclock, "ABC", steps[-1][0])
        lfeed.deliver()
    move(late, lfeed, lclock, "ABC", steps[-1][0])
    got, want = late.alerting._series[("ABC", "1m", "sma", 3)], ex.alerting._series[("ABC", "1m", "sma", 3)]
    assert got.seeded and not got.waiting and got.last_time == want.last_time
    assert got.calc.value == (pytest.approx(want.calc.value) if want.calc.value is not None else None)
    assert lfeed.asked_directly == 0


def test_rsi_level_and_macd_cross():
    ex, feed, clock, a = setup()
    rsi = ex.alerting.create({"kind": "indicator", "symbol": "ABC", "indicator": "rsi", "period": 5, "tf": "1m",
                              "op": "above", "value": "70"})
    macd = ex.alerting.create({"kind": "indicator", "symbol": "ABC", "indicator": "macd", "tf": "1m",
                               "op": "crosses_above"})
    bars_minute(ex, feed, clock, [100 - i * 0.5 for i in range(40)])  # a steady fall
    assert rsi["status"] == "active" and macd["status"] == "active"
    s = ex.alerting._series[("ABC", "1m", "macd", 26)]
    assert s.diff is not None and s.diff < 0
    bars_minute(ex, feed, clock, [80 + i for i in range(12)])  # a sharp rise
    assert rsi["status"] == "triggered" and macd["status"] == "triggered"


def test_indicator_tick_cost_is_constant_until_a_bar_closes(monkeypatch):
    ex, feed, clock, a = setup()
    ex.alerting.create({"kind": "indicator", "symbol": "ABC", "indicator": "sma", "period": 5, "tf": "1h",
                        "op": "crosses_above"})
    calls = []
    real = feed.bars
    monkeypatch.setattr(feed, "bars", lambda *a, **k: calls.append(a) or real(*a, **k))
    clock.set(clock.now() // 3600 * 3600 + 5)
    for _ in range(50):
        move(ex, feed, clock, "ABC", "100")
    assert len(calls) <= 1  # at most the bar that closed when the hour turned


# ---- options -----------------------------------------------------------------------------------------
def test_option_delta_and_iv_alerts_on_model_prices():
    ex, feed, clock, a = setup()
    exp = date(2026, 12, 18)
    sym = build("ABC", exp, "C", D("100"))
    delta = ex.alerting.create({"kind": "option", "symbol": sym, "metric": "delta", "op": "above", "value": "0.8"})
    c = __import__("app.options.symbology", fromlist=["parse"]).parse(sym)
    iv_now = chain.option_snapshot(c, 100.0, feed.base_vol("ABC"), ex.now())["iv"] * 100
    iv = ex.alerting.create({"kind": "option", "symbol": sym, "metric": "iv", "op": "above",
                             "value": str(round(iv_now + 0.5, 2))})
    move(ex, feed, clock, "ABC", "101")
    assert delta["status"] == "active"
    move(ex, feed, clock, "ABC", "115")
    assert delta["status"] == "triggered"
    assert iv["status"] == "triggered"  # the model's volatility smile rises away from the money
    [n] = [n for n in notes(ex, a, "alert") if n["alert_id"] == delta["id"]]
    assert n["symbol"] == "ABC" and n["text"].startswith("Delta of ABC 18 Dec 2026 100 Call rises to 0.8: now 0.9")


def test_option_alert_validation():
    ex, feed, clock, a = setup()
    with pytest.raises(ValueError, match="option contract symbol"):
        ex.alerting.create({"kind": "option", "symbol": "ABC", "metric": "iv", "op": "above", "value": "40"})
    with pytest.raises(ValueError, match="already expired"):
        ex.alerting.create({"kind": "option", "symbol": build("ABC", date(2026, 1, 16), "C", D("100")),
                            "metric": "iv", "op": "above", "value": "40"})
    with pytest.raises(ValueError, match="delta must be between -1 and 1"):
        ex.alerting.create({"kind": "option", "symbol": build("ABC", date(2026, 12, 18), "C", D("100")),
                            "metric": "delta", "op": "above", "value": "2"})


# ---- positions and the account -----------------------------------------------------------------------
def test_position_and_account_alerts():
    ex, feed, clock, a = setup()
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})  # 100 x 100.00
    pnl = ex.alerting.create({"kind": "position", "account_id": a, "symbol": "ABC", "metric": "unrealized_pnl",
                              "op": "below", "value": "-500"})
    pct = ex.alerting.create({"kind": "position", "account_id": a, "symbol": "ABC", "metric": "unrealized_pct",
                              "op": "above", "value": "10"})
    eq = ex.alerting.create({"kind": "account", "account_id": a, "metric": "equity", "op": "below", "value": "99500"})
    excess = ex.alerting.create({"kind": "account", "account_id": a, "metric": "margin_excess", "op": "below",
                                 "value": "90000"})
    bp = ex.alerting.create({"kind": "account", "account_id": a, "metric": "buying_power", "op": "below",
                             "value": "199000"})
    move(ex, feed, clock, "ABC", "96")  # -400
    assert pnl["status"] == "active" and pnl["last_value"] == "-400.00"
    assert bp["status"] == "triggered"  # buying power fell from 200,000 when 10,000 of stock was bought
    move(ex, feed, clock, "ABC", "94.99")  # -501
    assert pnl["status"] == "triggered" and eq["status"] == "triggered" and excess["status"] == "active"
    move(ex, feed, clock, "ABC", "110.01")
    assert pct["status"] == "triggered"
    with pytest.raises(ValueError, match="needs an account"):
        ex.alerting.create({"kind": "account", "metric": "equity", "op": "below", "value": "1"})


def test_position_alert_without_a_position_waits():
    ex, feed, clock, a = setup()
    al = ex.alerting.create({"kind": "position", "account_id": a, "symbol": "ABC", "metric": "unrealized_pnl",
                             "op": "above", "value": "0"})
    move(ex, feed, clock, "ABC", "120")
    assert al["status"] == "active" and al["last_value"] == "no position"


# ---- reminders, expiry, calendar ------------------------------------------------------------------------
def test_one_shot_and_repeating_reminders_on_the_simulated_clock():
    ex, feed, clock, a = setup()
    once = ex.alerting.create({"kind": "reminder", "at": T0 + 120, "message": "Check the open"})
    every = ex.alerting.create({"kind": "reminder", "at": T0 + 60, "message": "Stretch", "repeat": True,
                                "cooldown": 300})
    clock.set(T0 + 59)
    ex.tick()
    assert ex.alerting.notices.list(a) == []
    clock.set(T0 + 60)
    ex.tick()
    clock.set(T0 + 120)
    ex.tick()
    assert once["status"] == "triggered" and every["fire_count"] == 1 and every["status"] == "active"
    clock.set(T0 + 360)
    ex.tick()
    assert every["fire_count"] == 2
    clock.set(T0 + 360 + 3000)  # a replay seek over ten intervals: one reminder, not ten
    ex.tick()
    assert every["fire_count"] == 3 and every["rearm_at"] > clock.now()
    texts = [n["text"] for n in notes(ex, a, "reminder")]
    assert texts.count("Stretch") == 3 and texts.count("Check the open") == 1
    with pytest.raises(ValueError, match="in the future"):
        ex.alerting.create({"kind": "reminder", "at": T0, "message": "late"})
    with pytest.raises(ValueError, match="needs a message"):
        ex.alerting.create({"kind": "reminder", "at": T0 + 99999})


def test_alert_expires_and_says_so():
    ex, feed, clock, a = setup()
    al = ex.alerting.create({"kind": "price", "symbol": "ABC", "op": "above", "price": "200",
                             "expires_at": T0 + 30})
    move(ex, feed, clock, "ABC", "101", seconds=29)
    assert al["status"] == "active"
    move(ex, feed, clock, "ABC", "201", seconds=2)
    assert al["status"] == "expired" and al["fire_count"] == 0
    [n] = notes(ex, a, "alert")
    assert n["title"] == "Alert expired"
    with pytest.raises(ValueError, match="has expired"):
        ex.alerting.update(al["id"], {"status": "active"})


def test_calendar_alert_for_held_symbols():
    ex, feed, clock, a = setup()
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    feed.schedule(dividend("ABC", EX + timedelta(days=3), EX + timedelta(days=5), "0.40"))
    feed.schedule(dividend("XYZ", EX + timedelta(days=1), EX + timedelta(days=5), "0.40"))  # not held
    al = ex.alerting.create({"kind": "calendar", "account_id": a, "days_before": 2, "repeat": True})
    for _ in range(6):
        clock.advance(86400)
        ex.tick()
    [n] = [n for n in notes(ex, a, "alert") if n["alert_id"] == al["id"]]
    assert n["title"] == "Upcoming dividend: ABC" and "the ex-date is in 2 days (2026-11-28)" in n["text"]
    assert al["fire_count"] == 1 and al["status"] == "active"


def test_calendar_alert_is_tried_again_when_the_feed_cannot_list_actions_yet():
    """A live feed loads corporate actions in the background and refuses until it has them. The alert
    must not wait for the next day."""
    ex, feed, clock, a = setup()
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    feed.schedule(dividend("ABC", EX + timedelta(days=1), EX + timedelta(days=5), "0.40"))
    al = ex.alerting.create({"kind": "calendar", "account_id": a, "days_before": 2, "repeat": True})
    real, state = feed.corporate_actions, {"pending": True, "asked": 0}

    def actions(start, end):
        if end - start != timedelta(days=2):  # the lifecycle's own questions
            return real(start, end)
        state["asked"] += 1
        if state["pending"]:
            raise RuntimeError("corporate actions are still loading")
        return real(start, end)
    feed.corporate_actions = actions
    clock.advance(86400)  # 2026-11-24
    ex.tick()
    assert al["last_value"] == "calendar unavailable: corporate actions are still loading" and al["fire_count"] == 0
    asked = state["asked"]
    for _ in range(4):
        move(ex, feed, clock, "ABC", "100")
    assert asked == 1 and state["asked"] == 1  # not asked again on every tick
    for _ in range(4):
        move(ex, feed, clock, "ABC", "100")
    assert state["asked"] == 2 and al["fire_count"] == 0  # but again a few seconds later
    state["pending"] = False
    for _ in range(6):
        move(ex, feed, clock, "ABC", "100")
    assert al["fire_count"] == 1 and not ex.alerting._cal_waiting
    [n] = [n for n in notes(ex, a, "alert") if n["alert_id"] == al["id"]]
    assert n["title"] == "Upcoming dividend: ABC"


# ---- persistence and migration ------------------------------------------------------------------------
def test_alerts_and_notifications_survive_a_restart(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("Margin", "margin").id
    done = ex.alerting.create({"kind": "price", "symbol": "ABC", "op": "above", "price": "101"})
    rep = ex.alerting.create({"kind": "price", "symbol": "ABC", "op": "above", "price": "101", "repeat": True,
                              "cooldown": 30, "message": "take profit?"})
    waiting = ex.alerting.create({"kind": "price", "symbol": "ABC", "op": "below", "price": "90"})
    move(ex, feed, clock, "ABC", "102")
    first = notes(ex, a)
    ex.alerting.notices.mark_read(a, [first[0]["id"]])
    ex.save(force=True)

    ex2 = Exchange(SimFeed(), SimClock(clock.now()), data_dir=str(tmp_path))
    assert ex2.load()
    ex2.feed.set_quote("ABC", D("101.99"), D("102"), D("102"))
    ex2.tick()
    assert ex2.alerting.get(done["id"])["status"] == "triggered" and ex2.alerting.get(rep["id"])["fire_count"] == 1
    assert [n["id"] for n in notes(ex2, a)] == [n["id"] for n in first]
    assert ex2.alerting.notices.unread(a) == len(first) - 1
    ex2.feed.set_quote("ABC", D("88.99"), D("89"), D("89"))
    ex2.clock.advance(1)
    ex2.tick()
    assert ex2.alerting.get(waiting["id"])["status"] == "triggered"  # the index was rebuilt on load
    assert ex2.alerting.get(rep["id"])["armed"] is True
    ex2.feed.set_quote("ABC", D("101.99"), D("102"), D("102"))
    ex2.clock.advance(60)
    ex2.tick()
    assert ex2.alerting.get(rep["id"])["fire_count"] == 2
    n = notes(ex2, a)[0]
    assert n["id"] > first[0]["id"] and n["text"].endswith("Note: take profit?")


def test_old_state_file_and_old_alert_list_load(tmp_path):
    shutil.copy(os.path.join(FIXTURES, "state_main", "state.json"), tmp_path / "state.json")
    raw = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "notifications" not in raw and "kind" not in raw["alerts"][0]
    clock = SimClock(T0 + 120)
    ex = Exchange(SimFeed(), clock, data_dir=str(tmp_path))
    assert ex.load()
    [al] = ex.alerts
    assert al["id"] == "AL00001" and al["kind"] == "price" and al["price"] == "150" and al["status"] == "active"
    ex.feed.set_quote("ABC", D("150.99"), D("151"), D("151"))
    ex.tick()
    assert al["status"] == "triggered"
    assert all(len(notes(ex, x, "alert")) == 1 for x in ex.accounts)
    new = ex.add_alert("ABC", "below", "100")
    assert new["id"] == "AL00002"  # the stored sequence continues
    ex.save(force=True)
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert {x["id"] for x in saved["alerts"]} == {"AL00001", "AL00002"} and saved["notifications"]["logs"]


def test_migrate_keeps_a_triggered_old_alert_triggered():
    al = migrate({"id": "AL00007", "symbol": "ABC", "op": "below", "price": "10", "created_at": 1.0,
                  "triggered_at": 5.0, "note": "Adjusted from 20 for the split"})
    assert al["status"] == "triggered" and al["fire_count"] == 1 and al["note"].startswith("Adjusted")
    assert migrate("junk") is None


def test_notification_log_is_capped_per_account():
    ex, feed, clock, a = setup()
    for i in range(CAP + 40):
        ex.alerting.notices.add(a, "account", "info", "t", f"n{i}")
    log = ex.alerting.notices.logs[a]
    assert len(log) == CAP and log[0]["text"] == "n40" and log[-1]["text"] == f"n{CAP + 39}"


# ---- splits ---------------------------------------------------------------------------------------------
def test_split_adjusts_every_level_in_the_new_model():
    ex, feed, clock, a = setup()
    cooling = ex.alerting.create({"kind": "price", "symbol": "ABC", "op": "above", "price": "100", "repeat": True,
                                  "cooldown": 86400 * 5})
    move(ex, feed, clock, "ABC", "101")  # fired; in its cool-down
    assert cooling["fire_count"] == 1
    paused = ex.alerting.create({"kind": "price", "symbol": "ABC", "op": "below", "price": "50"})
    ex.alerting.update(paused["id"], {"status": "paused"})
    ref = ex.alerting.create({"kind": "change", "symbol": "ABC", "op": "above", "value": "5", "basis": "reference",
                              "reference": "100"})
    vol = ex.alerting.create({"kind": "volume", "symbol": "ABC", "value": "100000", "mode": "level"})
    opt = ex.alerting.create({"kind": "option", "symbol": build("ABC", date(2026, 12, 18), "C", D("100")),
                              "metric": "delta", "op": "above", "value": "0.9"})
    feed.schedule(split("ABC", EX, 2))
    clock.advance(86400)
    ex.tick()
    assert cooling["price"] == "50" and paused["price"] == "25" and ref["params"]["reference"] == "50"
    assert vol["value"] == "200000" and opt["status"] == "paused" and "Paused: the contract was adjusted" in opt["note"]
    move(ex, feed, clock, "ABC", "60")  # 120 before the split: far above the adjusted 52.50 reference
    assert ref["status"] == "triggered" and opt["fire_count"] == 0


# ---- replay isolation -------------------------------------------------------------------------------------
def test_alerts_made_in_a_replay_stay_in_the_replay(tmp_path):
    from app.marketdata.replay.samples import SAMPLES
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()), data_dir=str(tmp_path / "live"))
    aid = ex.create_account("Margin", "margin").id
    ex.tick()
    c = TestClient(create_app(ex, run_loop=False))
    c.post("/api/alert-rules", json={"kind": "price", "symbol": "NVLX", "op": "above", "price": 1})
    live_alerts = json.dumps(ex.alerts, sort_keys=True)
    assert c.post("/api/replay/start", json={"session": SAMPLES[0][0]}).status_code == 200
    rp = c.app.state.ctx.ex
    raid = list(rp.accounts)[0]
    sym = sorted(rp.feed.symbols())[0]
    last = rp.feed.quote(sym).last if rp.feed.quote(sym) else D("1")
    r = c.post("/api/alert-rules", json={"kind": "price", "symbol": sym, "op": "above", "price": str(last / 100)})
    assert r.status_code == 200, r.text
    rem = c.post("/api/alert-rules", json={"kind": "reminder", "in_minutes": 1, "message": "replay reminder"}).json()
    assert {a["id"] for a in c.get("/api/alert-rules").json()} == {r.json()["id"], rem["id"]}
    c.post("/api/replay/step", json={"seconds": 120})
    items = c.get(f"/api/notifications?account_id={raid}").json()["items"]
    assert any(n["text"] == "replay reminder" for n in items)
    c.post("/api/replay/exit")
    assert json.dumps(ex.alerts, sort_keys=True) == live_alerts
    assert c.get(f"/api/notifications?account_id={aid}").json()["items"] == []
    assert [a["symbol"] for a in c.get("/api/alert-rules").json()] == ["NVLX"]


# ---- bounded cost ----------------------------------------------------------------------------------------
def test_engine_cost_is_bounded_with_thousands_of_alerts():
    symbols = [f"S{i:03d}" for i in range(100)]
    ex, feed, clock = make_exchange(**{s: "100" for s in symbols})
    a = ex.create_account("Margin", "margin").id
    for i in range(5000):
        s = symbols[i % 100]
        ex.alerting.create({"kind": "price", "symbol": s, "op": "above", "price": "1000"})
    for i in range(500):
        ex.alerting.create({"kind": "reminder", "at": T0 + 10 ** 6 + i, "message": "later"})
    assert len(ex.alerts) == 5500
    ex.alerting.evaluations = 0
    start = time.perf_counter()
    for _ in range(50):
        move(ex, feed, clock, "S007", "101")  # one symbol changes per tick
    took = (time.perf_counter() - start) / 50
    assert ex.alerting.evaluations == 50 * 50  # only S007's 50 alerts, never the other 5,450
    assert took < 0.05, f"{took * 1000:.1f} ms per tick"
    with pytest.raises(ValueError, match="already"):
        for _ in range(model.MAX_ALERTS):
            ex.alerting.create({"kind": "price", "symbol": "S001", "op": "above", "price": "1000"})


# ---- system events ----------------------------------------------------------------------------------------
def test_system_events_become_notifications():
    ex, feed, clock, a = setup()
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    [o] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 5, "type": "limit", "limit_price": 50, "tif": "day"})
    ex.cancel(a, o.id)  # canceled by the user: no notification
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 3, "type": "limit", "limit_price": 50, "tif": "day"})
    feed.schedule(dividend("ABC", EX, EX + timedelta(days=1), "0.50"))
    clock.advance(86400)  # the day order expires; ABC goes ex-dividend
    ex.tick()
    clock.advance(86400)
    ex.tick()
    titles = [n["title"] for n in notes(ex, a)][::-1]
    assert titles[0] == "Order filled" and "Order canceled" not in titles
    assert "Order expired" in titles and "Ex-dividend" in titles and "Dividend" in titles
    [d] = notes(ex, a, "dividend")[:1]
    assert d["severity"] == "success" and d["text"] == "Dividend of 5.00 paid on ABC"
    # nothing here went to the ledger beyond the trade and the dividend
    kinds = [t.kind for t in ex.account(a).ledger.transactions]
    assert "notification" not in kinds


def test_option_expiry_and_split_notifications():
    ex, feed, clock, a = setup()
    exp = date(2026, 11, 27)
    sym = build("ABC", exp, "C", D("90"))
    ex.submit(a, {"symbol": sym, "side": "buy", "qty": 1})
    feed.schedule(split("ABC", EX, 2))
    clock.advance(86400)
    ex.tick()
    assert any(n["title"] == "Stock split applied" for n in notes(ex, a, "split"))
    clock.set(T0 + 86400 * 5)
    feed.set_quote("ABC", D("59.99"), D("60"), D("60"))
    ex.tick()
    opt = notes(ex, a, "option")
    assert opt and opt[0]["title"] in ("Option exercised", "Option closed at expiry", "Option expired")


def test_margin_call_notifications():
    ex, feed, clock, a = setup()
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1900})
    move(ex, feed, clock, "ABC", "63.10")
    [n] = notes(ex, a, "margin")
    assert n["severity"] == "critical" and n["title"] == "Margin call"
    move(ex, feed, clock, "ABC", "90")
    assert notes(ex, a, "margin")[0]["title"] == "Margin call cleared"


class FlakyFeed(SimFeed):
    stale = False

    def status_info(self):
        return {"stale": self.stale, "stream": {"state": "ready", "message": "connection reset"}}


def test_feed_disconnect_and_missing_prices_are_reported():
    clock = SimClock(T0)
    feed = FlakyFeed()
    ex = Exchange(feed, clock)
    feed.set_quote("ABC", D("99.99"), D("100"), D("100"))
    ex.tick()
    a = ex.create_account("Margin", "margin").id
    feed.stale = True
    clock.advance(10)
    ex.tick()
    [n] = notes(ex, a, "data")
    assert n["title"] == "Data feed disconnected" and "connection reset" in n["text"]
    feed.stale = False
    clock.advance(10)
    ex.tick()
    assert notes(ex, a, "data")[0]["title"] == "Data feed reconnected"
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1})
    feed.quotes.pop("ABC")
    clock.advance(20)
    ex.tick()
    assert notes(ex, a, "data")[0]["title"] == "Prices unavailable"


# ---- API -------------------------------------------------------------------------------------------------
@pytest.fixture
def api(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("Margin", "margin").id
    return TestClient(create_app(ex, run_loop=False)), ex, feed, clock, a


def test_api_alert_crud_pause_and_validation(api):
    c, ex, feed, clock, a = api
    r = c.post("/api/alert-rules", json={"kind": "price", "symbol": "abc", "op": "above", "price": 105,
                                         "message": "breakout", "account_id": a})
    assert r.status_code == 200
    al = r.json()
    assert al["description"] == "ABC rises to 105.00" and al["status"] == "active" and al["message"] == "breakout"
    assert c.post("/api/alert-rules", json={"kind": "price", "symbol": "ZZZ", "op": "above", "price": 1}).json() == \
        {"detail": "unknown symbol ZZZ"}
    assert c.post("/api/alert-rules", json={"kind": "nope"}).status_code == 400
    assert c.post("/api/alert-rules", json={"kind": "price", "symbol": "ABC", "op": "up", "price": 1}).json()["detail"] \
        == "condition must be one of: above, below"
    r = c.patch(f"/api/alert-rules/{al['id']}", json={"price": "110", "repeat": True, "cooldown": 120})
    assert r.json()["price"] == "110" and r.json()["repeat"] and r.json()["id"] == al["id"]
    assert c.patch(f"/api/alert-rules/{al['id']}", json={"status": "paused"}).json()["status"] == "paused"
    move(ex, feed, clock, "ABC", "120")
    assert ex.alerting.get(al["id"])["fire_count"] == 0  # paused alerts are not evaluated
    assert c.patch(f"/api/alert-rules/{al['id']}", json={"status": "active"}).json()["status"] == "active"
    move(ex, feed, clock, "ABC", "121")
    assert ex.alerting.get(al["id"])["fire_count"] == 1
    assert [x["id"] for x in c.get(f"/api/alert-rules?account_id={a}").json()] == [al["id"]]
    assert c.get("/api/alert-rules?account_id=OTHER").json() == []
    assert c.delete(f"/api/alert-rules/{al['id']}").json() == {"ok": True}
    assert c.delete(f"/api/alert-rules/{al['id']}").status_code == 404
    meta = c.get("/api/alert-rules/meta").json()
    assert "indicator" in meta["kinds"] and meta["earnings"] is False


def test_api_legacy_alert_routes_still_work(api):
    c, ex, feed, clock, a = api
    al = c.post("/api/alerts", json={"symbol": "ABC", "op": "below", "price": 90}).json()
    assert al["kind"] == "price" and al["triggered_at"] is None
    assert [x["id"] for x in c.get("/api/alerts").json()] == [al["id"]]
    assert c.get("/api/alert-rules").json()[0]["description"] == "ABC falls to 90.00"


def test_api_notifications_catch_up_filters_and_read_state(api):
    c, ex, feed, clock, a = api
    c.post("/api/alert-rules", json={"kind": "price", "symbol": "ABC", "op": "above", "price": 101})
    c.post(f"/api/accounts/{a}/orders", json={"symbol": "ABC", "side": "buy", "qty": 2})
    move(ex, feed, clock, "ABC", "102")
    r = c.get(f"/api/notifications?account_id={a}").json()
    assert r["unread"] == 2 and [n["category"] for n in r["items"]] == ["alert", "fill"]
    first = r["items"][-1]["id"]
    assert [n["category"] for n in c.get(f"/api/notifications?account_id={a}&after={first}").json()["items"]] == ["alert"]
    assert [n["category"] for n in c.get(f"/api/notifications?account_id={a}&category=fill").json()["items"]] == ["fill"]
    assert c.post("/api/notifications/read", json={"account_id": a, "ids": [first]}).json() == {"unread": 1}
    assert len(c.get(f"/api/notifications?account_id={a}&unread=true").json()["items"]) == 1
    assert c.post("/api/notifications/read", json={"account_id": a}).json() == {"unread": 0}
    assert c.post("/api/notifications/read", json={"account_id": a, "ids": [first], "read": False}).json() == {"unread": 1}
    assert c.get("/api/notifications?account_id=NOPE").json() == {"items": [], "unread": 0, "mode": "live",
                                                                   "account_found": False}
    assert c.post("/api/notifications/read", json={"account_id": "NOPE"}).status_code == 404
    assert c.delete(f"/api/notifications?account_id={a}").json()["unread"] == 0


def test_notifications_reach_the_websocket(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("Margin", "margin").id
    with TestClient(create_app(ex, run_loop=True, tick_seconds=0.02)) as c:
        c.post("/api/alert-rules", json={"kind": "price", "symbol": "ABC", "op": "above", "price": 101})
        with c.websocket_connect("/ws") as ws:
            ws.send_json({"symbols": ["ABC"], "account": a})
            ws.receive_json()
            feed.set_quote("ABC", D("101.99"), D("102"), D("102"))
            got = []
            for _ in range(200):
                msg = ws.receive_json()
                got += [e for e in msg.get("events", []) if e["type"] == "notification"]
                if got:
                    break
    [e] = got
    assert e["account_id"] == a and e["notification"]["text"] == "ABC rises to 101.00: now 102.00."
