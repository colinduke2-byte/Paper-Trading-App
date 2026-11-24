"""Regression tests for the review of PR 6 (one or more per finding)."""
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.clock import SimClock
from app.core.models import Fill
from app.exchange import Exchange, OrderRejected
from app.lifecycle.actions import dividend, split
from app.lifecycle.manager import uncovered
from app.marketdata.feed import SimFeed
from app.marketdata.synthetic import SyntheticFeed
from app.options import chain
from app.options.symbology import build
from app.core.money import cents
from helpers import D, make_exchange, move

EX = date(2026, 11, 25)  # the first day roll after T0
EXPIRY = chain.expirations(date(2026, 11, 24))[-1]


def acct(ex, type="margin", cash="100000", **settings):
    a = ex.create_account("t", type, D(cash))
    a.settings.update(settings)
    return a.id


def deep(ex, feed, clock, price, symbol="ABC", seconds=1):
    move(ex, feed, clock, symbol, price, seconds=seconds, bid_size=100000, ask_size=100000)


def logs(ex, a, kind):
    return [msg for _, k, msg in ex.account(a).life.log if k == kind]


def utc(y, mo, d, h=12):
    return datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp()


def put_fill(acc, sym, qty, price, n=[0]):
    n[0] += 1
    acc.apply_fill(Fill(f"P{n[0]}", None, acc.id, sym, "buy" if qty > 0 else "sell", D(abs(qty)), D(price), 0.0),
                   100 if len(sym) > 6 else 1)


# ---- 1: catch-up across a split then a dividend ------------------------------------------------
def _synthetic_run(step_days):
    start = utc(2027, 9, 1)
    clock = SimClock(start)
    feed = SyntheticFeed(seed=7, now=start, history_days=30)
    for info in feed.info.values():
        info["vol"] = 0.0  # no random walk, so prices only move by corporate actions
    ex = Exchange(feed, clock)
    ex.tick()
    a = acct(ex, "cash", "100000")
    ex.submit(a, {"symbol": "RAIL", "side": "buy", "qty": 100})
    end = utc(2027, 12, 5)
    while clock.now() < end:
        clock.set(min(clock.now() + 86400 * step_days, end))
        ex.tick()
    acc = ex.account(a)
    return acc.cash, acc.positions(), feed.quote("RAIL").last, [t.postings for t in acc.ledger.transactions
                                                               if t.kind == "dividend"]


def test_offline_over_split_then_dividend_equals_daily_ticking():
    daily = _synthetic_run(1)
    offline = _synthetic_run(200)
    assert offline == daily
    cash, pos, last, divs = daily
    assert pos == {"RAIL": 200}  # 2-for-1 on 2027-09-15
    assert divs == [[("cash", D("130.00")), ("income:dividends", D("-130.00"))]]  # 0.65 x 200, not 1.30 x 200


# ---- 2: replace() must apply the settled-cash rule -----------------------------------------------
def test_replace_cannot_bypass_the_settled_cash_rule():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "10000")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 50})
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 50})  # 4,999.50 unsettled, 5,000 settled
    [o] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "limit", "limit_price": "0.01"})
    with pytest.raises(OrderRejected, match="Cash accounts can only buy with settled cash"):
        ex.replace(a, o.id, {"qty": 99, "limit_price": "100"})
    assert (o.qty, o.limit_price) == (D("1"), D("0.01"))  # rolled back
    ex.replace(a, o.id, {"qty": 40, "limit_price": "100"})  # 4,000 of settled cash is fine
    move(ex, feed, clock, "ABC", "100")
    assert ex.account(a).position("ABC") == 40 and ex.summary(a)["settled_cash"] >= 0


# ---- 4: a missing quote must never cause a margin call or liquidation ------------------------------
def test_missing_quote_pauses_margin_checks_instead_of_liquidating():
    ex, feed, clock = make_exchange(ABC="100", XYZ="50")
    deep(ex, feed, clock, "100")
    deep(ex, feed, clock, "50", "XYZ")
    a = acct(ex, margin_call_grace_hours="0")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 900})
    ex.submit(a, {"symbol": "XYZ", "side": "buy", "qty": 900})
    del feed.quotes["XYZ"]
    for _ in range(5):
        clock.advance(3600)
        ex.tick()
    s = ex.summary(a)
    assert s["prices_unavailable"] == ["XYZ"] and not s["margin_call"] and s["margin_call_since"] is None
    assert ex.account(a).positions() == {"ABC": 900, "XYZ": 900}
    assert not logs(ex, a, "margin_call") and not logs(ex, a, "liquidation")
    assert len([e for e in ex.drain_events() if e.get("kind") == "prices_unavailable"]) == 1  # said once
    deep(ex, feed, clock, "50", "XYZ")
    assert ex.summary(a)["prices_unavailable"] == [] and not ex.summary(a)["margin_call"]


def test_split_without_a_price_settles_options_at_cost():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    call = build("ABC", EXPIRY, "C", D("100"))
    ex.submit(a, {"symbol": call, "side": "buy", "qty": 1})
    basis = ex.account(a).cost_basis(call)
    del feed.quotes["ABC"]
    feed.schedule(split("ABC", EX, 1, 2))
    clock.advance(86400)
    ex.tick()
    acc = ex.account(a)
    assert not acc.positions() and acc.fills[-1].kind == "split_settlement"
    assert acc.fills[-1].price * 100 == basis  # no phantom gain or loss from a zero mark


# ---- 5: liquidation must never uncover a short option -----------------------------------------------
def test_liquidation_buys_back_covered_calls_before_selling_the_stock():
    ex, feed, clock = make_exchange()
    a = acct(ex, "margin", "1000")
    acc = ex.account(a)
    call = build("ABC", EXPIRY, "C", D("110"))
    put_fill(acc, "ABC", 1500, "100")
    put_fill(acc, call, -2, "3.00")
    acc.ledger.post(0, "deposit", "test", [("cash", D("-80000")), ("equity:capital", D("80000"))])
    deep(ex, feed, clock, "48")
    before = uncovered(acc.positions())
    steps = ex.lifecycle.liquidate(acc, clock.now())
    # The stock is sold down to the 200 shares that cover the calls; if more must go, the calls are
    # bought back before those last 200 shares are sold.
    assert steps[0]["symbol"] == "ABC" and steps[0]["positions"]["ABC"] >= 200
    seen_call_buyback = False
    for st in steps:
        seen_call_buyback |= st["symbol"] == call
        if st["symbol"] == "ABC" and st["positions"].get("ABC", 0) < 200:
            assert seen_call_buyback
        assert all(v <= before.get(k, 0) for k, v in uncovered(st["positions"]).items())
    pos = acc.positions()
    assert call not in pos or pos.get("ABC", 0) >= 200


def test_coverage_counts_stock_and_later_dated_long_options():
    near, far = chain.expirations(date(2026, 11, 24))[0], EXPIRY
    sc, lc_far, lc_near = (build("ABC", near, "C", D("100")), build("ABC", far, "C", D("120")),
                           build("ABC", near, "C", D("90")))
    assert uncovered({sc: D(-2), "ABC": D(150)}) == {("ABC", "C"): 1}
    assert uncovered({sc: D(-2), lc_far: D(2)}) == {("ABC", "C"): 0}  # later-dated long covers
    far_short = build("ABC", far, "C", D("100"))
    assert uncovered({far_short: D(-1), lc_near: D(1)}) == {("ABC", "C"): 1}  # an earlier long does not
    sp = build("ABC", far, "P", D("90"))
    assert uncovered({sp: D(-1), "ABC": D(-100)}) == {("ABC", "P"): 0}


# ---- 6: a lost market snapshot must not undo a split ------------------------------------------------
def test_split_survives_a_lost_market_snapshot(tmp_path):
    start = utc(2027, 9, 14)
    clock = SimClock(start)
    feed = SyntheticFeed(seed=7, now=start, history_days=30)
    ex = Exchange(feed, clock, data_dir=str(tmp_path))
    ex.tick()
    a = acct(ex)
    ex.submit(a, {"symbol": "RAIL", "side": "buy", "qty": 100})
    clock.set(utc(2027, 9, 15))
    ex.tick()
    assert ex.account(a).position("RAIL") == 200
    eq = ex.summary(a)["equity"]
    ex.save(force=True)
    os.remove(tmp_path / "market.json")
    fresh = SyntheticFeed(seed=7, now=clock.now(), history_days=30)
    unadjusted = fresh.quote("RAIL").last
    ex2 = Exchange(fresh, SimClock(clock.now()), data_dir=str(tmp_path))
    assert ex2.load()
    assert "RAIL-split-2027-09-15" in fresh.applied_actions
    assert fresh.quote("RAIL").last == cents(unadjusted / 2)
    assert abs(ex2.summary(a)["equity"] - eq) < eq * D("0.02")  # not doubled
    ex2.save(force=True)
    data = json.loads((tmp_path / "market.json").read_text())
    assert "RAIL-split-2027-09-15" in data["applied_actions"]


# ---- 7: the ex-dividend drop must not trigger stops or dent equity ---------------------------------
def test_dividend_lowers_stops_and_buy_limits_and_counts_in_equity():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
    [stop] = ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 50, "type": "stop", "stop_price": "99.50", "tif": "gtc"})
    [trail] = ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 50, "type": "trailing_stop", "trail_amount": "1",
                            "tif": "gtc"})
    [bl] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "limit_price": "95", "tif": "gtc"})
    [sl] = ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 10, "type": "limit", "limit_price": "120", "tif": "gtc"})
    move(ex, feed, clock, "ABC", "100")
    assert trail.trail_ref == D("100") and trail.stop_price == D("99.00")
    before = ex.summary(a)["equity"]
    feed.schedule(dividend("ABC", EX, EX + timedelta(days=3), "1.00"))
    clock.advance(86400)
    ex.tick()
    assert stop.stop_price == D("98.50") and bl.limit_price == D("94") and sl.limit_price == D("120")
    assert trail.trail_ref == D("99") and trail.stop_price == D("98.00")
    assert stop.reason == "Stop price lowered by the 1 dividend (ex-date 2026-11-25)"
    move(ex, feed, clock, "ABC", "99")  # the stock opens lower by the dividend
    assert stop.active and trail.active
    s = ex.summary(a)
    assert s["pending_dividends"] == D("100.00") and s["equity"] == before  # 100 x 99 + 100 receivable
    clock.advance(3 * 86400)
    ex.tick()
    s = ex.summary(a)
    assert s["pending_dividends"] == 0 and s["equity"] == before


# ---- 8: alerts are split-adjusted ---------------------------------------------------------------
def test_alerts_are_split_adjusted():
    ex, feed, clock = make_exchange()
    below = ex.add_alert("ABC", "below", "80")
    above = ex.add_alert("ABC", "above", "120.10")
    feed.schedule(split("ABC", EX, 4))
    clock.advance(86400)
    ex.tick()
    move(ex, feed, clock, "ABC", "25")
    assert below["price"] == "20" and above["price"] == "30.025" and below["triggered_at"] is None
    assert below["note"] == "Adjusted from 80 for the 4-for-1 stock split of ABC on 2026-11-25"


# ---- 9: margin-call flapping ----------------------------------------------------------------------
def test_margin_call_flapping_keeps_the_start_time_and_does_not_spam():
    ex, feed, clock = make_exchange()
    deep(ex, feed, clock, "100")
    a = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1900})  # call below ~63.16
    deep(ex, feed, clock, "63.10")
    since = ex.summary(a)["margin_call_since"]
    n_txns = len(ex.account(a).ledger.transactions)
    for i in range(600):
        deep(ex, feed, clock, "63.20" if i % 2 else "63.10")
    assert ex.summary(a)["margin_call_since"] == since
    assert len(ex.account(a).ledger.transactions) == n_txns
    assert len(logs(ex, a, "margin_call")) == 1
    clock.set(since + 48 * 3600)
    deep(ex, feed, clock, "63.10", seconds=0)
    assert ex.account(a).position("ABC") < 1900  # the grace period was not restarted


def test_liquidation_paused_is_said_once():
    ex, feed, clock = make_exchange()
    a = acct(ex, margin_call_grace_hours="0")
    acc = ex.account(a)
    far_otm = build("ABC", EXPIRY, "C", D("400"))
    put_fill(acc, far_otm, 5, "0.01")
    acc.ledger.post(0, "deposit", "test", [("cash", D("-100100")), ("equity:capital", D("100100"))])
    assert ex.quote(far_otm).bid == 0
    for _ in range(5):
        clock.advance(1)
        ex.tick()
    assert len(logs(ex, a, "liquidation")) == 1 and "paused" in logs(ex, a, "liquidation")[0]


# ---- 11: small consistency fixes ---------------------------------------------------------------
def test_small_fixes():
    ex, feed, clock = make_exchange()
    deep(ex, feed, clock, "100")
    a = acct(ex)
    drip = acct(ex, dividend_reinvestment=True)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1001})  # debit 100.00: interest carry ends slightly negative
    ex.submit(drip, {"symbol": "ABC", "side": "buy", "qty": 100})
    feed.schedule(split("ABC", EX, 2))
    feed.schedule(dividend("ABC", EX + timedelta(days=1), EX + timedelta(days=2), "0.50"))
    clock.set(utc(2026, 12, 1, 0))
    ex.tick()
    assert str(ex.summary(a)["accrued_interest"]) == "0.00"  # never "-0.00"
    assert str(ex.account(a).position("ABC")) == "2002" and str(ex.positions(a)[0]["qty"]) == "2002"
    events = ex.drain_events()
    assert any(e["type"] == "fill" and e["fill"].kind == "reinvestment" for e in events)


def test_exercise_proceeds_settle_t_plus_1():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    exp = chain.expirations(date(2026, 11, 24))[0]
    put = build("ABC", exp, "P", D("110"))
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
    ex.submit(a, {"symbol": put, "side": "buy", "qty": 1})
    clock.set(chain.Contract(put, "ABC", exp, "P", D("110")).expiry_ts + 5)
    ex.tick()
    acc = ex.account(a)
    assert acc.position("ABC") == 0 and acc.fills[-1].kind == "exercise"
    [entry] = [e for e in acc.life.unsettled if e[2] == acc.fills[-1].id]
    assert entry[1] == D("11000.00")


# ---- replay: the clock may jump hours in one step ------------------------------------------------------
@pytest.mark.parametrize("hours", [1, 7, 30, 24 * 9])
def test_lifecycle_handles_any_clock_jump(hours):
    ex, feed, clock = make_exchange()
    deep(ex, feed, clock, "100")
    a = acct(ex, margin_interest_rate="7.2")
    c = acct(ex, "cash", "10000")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1500})  # debit 50,000: 10.00 a day
    ex.submit(c, {"symbol": "ABC", "side": "buy", "qty": 10})
    ex.submit(c, {"symbol": "ABC", "side": "sell", "qty": 10})
    feed.schedule(dividend("ABC", EX, EX + timedelta(days=2), "0.10"))
    end = utc(2026, 12, 1, 6)
    while clock.now() < end:
        clock.set(min(clock.now() + hours * 3600, end))
        ex.tick()
    acc = ex.account(a)
    # Nov 24-26 on 50,000 (3 x 10.00) and Nov 27-30 on 49,850 after the 150.00 dividend (4 x 9.97): posted once
    assert [t.postings[0] for t in acc.ledger.transactions if t.kind == "interest"] == \
           [("expense:margin_interest", D("69.88"))]
    assert [t.postings[0] for t in acc.ledger.transactions if t.kind == "dividend"] == [("cash", D("150.00"))]
    assert ex.summary(c)["unsettled_cash"] == 0
