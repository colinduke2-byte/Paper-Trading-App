"""Regression tests for the independent review of the live provider gaps (PR 17), findings 1 to 4.

Each test starts from the reviewer's repro."""
import json
from datetime import date

from app.core import calendar as cal
from app.core.money import D
from app.exchange import Exchange, OrderRejected
from app.marketdata.feed import SimFeed
from app.core.clock import SimClock
from app.options import chain as optchain
from app.options.symbology import build

from helpers import make_exchange
from test_provider_actions import Live, ca_body, ny

TSLA_SPLIT_ID = "alpaca:189bd849-ab9f-4b4d-aaaa-a6d415fd976d"


# ---- finding 1: a late corporate action only reaches positions held over its ex-date --------------------
def _late_split_lv(body_state):
    lv = Live(ny("2024-04-22", "10:00"), lambda req: (200, body_state["body"]))
    a = lv.ex.create_account("Cash", "cash")
    lv.quote("AAPL", "99.99", "100.00", "100.00")
    lv.quote("TSLA", "99.99", "100.00", "100.00")
    lv.ex.tick()
    return lv, a


def test_late_window_applies_old_split_to_position_bought_after_ex_date():
    # The reviewer's repro, unchanged: TSLA 2-for-1 with ex-date 2024-04-24, TSLA bought on the 25th.
    body = json.dumps(ca_body({"split": "2024-04-24"}), default=str)
    lv = Live(ny("2024-04-22", "10:00"), body)
    a = lv.ex.create_account("Cash", "cash")
    lv.quote("AAPL", "99.99", "100.00", "100.00"); lv.ex.tick()
    lv.ex.submit(a.id, {"symbol": "AAPL", "side": "buy", "qty": 1})
    lv.goto(ny("2024-04-22", "11:00"))
    lv.goto(ny("2024-04-24", "09:35"))          # ex-date passes, TSLA not held
    lv.goto(ny("2024-04-25", "10:00"))
    lv.quote("TSLA", "99.99", "100.00", "100.00"); lv.ex.tick()
    (o,) = lv.ex.submit(a.id, {"symbol": "TSLA", "side": "buy", "qty": 10}); assert o.status == "filled"
    # holds(): no split_pending pause for an account that did not hold TSLA over the ex-date
    assert lv.ex.summary(a.id)["margin_paused"] == []
    lv.goto(ny("2024-04-25", "10:01"))
    lv.goto(ny("2024-04-26", "09:35"))
    assert lv.ex.account(a.id).position("TSLA") == 10
    assert lv.ex.quote("TSLA").last == D("100.00")  # the live quote is not rescaled either
    events = lv.ex.drain_events()
    assert any(e.get("kind") == "corporate_action_skipped" and "TSLA" in e["message"] for e in events)
    lv.goto(ny("2024-04-29", "09:35"))  # applied once: never reconsidered on later days of the late window
    assert lv.ex.account(a.id).position("TSLA") == 10 and TSLA_SPLIT_ID in lv.ex.lifecycle.applied


def test_late_split_published_after_ex_date_reaches_only_the_account_that_held_it():
    state = {"body": json.dumps(ca_body({}), default=str)}
    lv, held = _late_split_lv(state)
    late = lv.ex.create_account("Later buyer", "cash")
    (o,) = lv.ex.submit(held.id, {"symbol": "TSLA", "side": "buy", "qty": 10}); assert o.status == "filled"
    lv.goto(ny("2024-04-22", "11:00"))
    lv.goto(ny("2024-04-24", "09:35"))  # ex-date starts with nothing published
    lv.quote("TSLA", "99.99", "100.00", "100.00"); lv.ex.tick()
    (o,) = lv.ex.submit(late.id, {"symbol": "TSLA", "side": "buy", "qty": 7}); assert o.status == "filled"
    state["body"] = json.dumps(ca_body({"split": "2024-04-24"}), default=str)  # published late
    lv.goto(ny("2024-04-25", "09:35"))
    assert lv.ex.account(held.id).position("TSLA") == 20  # held over the ex-date: split
    assert lv.ex.account(late.id).position("TSLA") == 7  # bought after it: untouched
    lv.goto(ny("2024-04-26", "09:35"))
    assert lv.ex.account(held.id).position("TSLA") == 20  # exactly once


def test_late_split_adjusts_only_the_shares_held_over_the_ex_date():
    state = {"body": json.dumps(ca_body({}), default=str)}
    lv, a = _late_split_lv(state)
    (o,) = lv.ex.submit(a.id, {"symbol": "TSLA", "side": "buy", "qty": 10}); assert o.status == "filled"
    lv.goto(ny("2024-04-22", "11:00"))
    lv.goto(ny("2024-04-24", "09:35"))
    lv.quote("TSLA", "49.99", "50.00", "50.00"); lv.ex.tick()  # the market already trades post-split
    (o,) = lv.ex.submit(a.id, {"symbol": "TSLA", "side": "buy", "qty": 4}); assert o.status == "filled"
    state["body"] = json.dumps(ca_body({"split": "2024-04-24"}), default=str)
    lv.goto(ny("2024-04-25", "09:35"))
    acct = lv.ex.account(a.id)
    assert acct.position("TSLA") == 24  # 10 held over the ex-date became 20; the 4 bought after stay 4
    assert [l.qty for l in acct.lots["TSLA"]] == [20, 4]
    assert [l.price for l in acct.lots["TSLA"]] == [D("50"), D("50")]
    assert acct.cost_basis("TSLA") == D("1200.00")
    lv.goto(ny("2024-04-26", "09:35"))
    assert lv.ex.account(a.id).position("TSLA") == 24


def test_late_split_not_applied_to_a_position_sold_before_and_bought_back_after_the_ex_date():
    state = {"body": json.dumps(ca_body({}), default=str)}
    lv, a = _late_split_lv(state)
    (o,) = lv.ex.submit(a.id, {"symbol": "TSLA", "side": "buy", "qty": 10}); assert o.status == "filled"
    lv.goto(ny("2024-04-22", "11:00"))
    lv.goto(ny("2024-04-23", "10:00"))
    lv.quote("TSLA", "99.99", "100.00", "100.00"); lv.ex.tick()
    (o,) = lv.ex.submit(a.id, {"symbol": "TSLA", "side": "sell", "qty": 10}); assert o.status == "filled"
    lv.goto(ny("2024-04-24", "09:35"))  # ex-date: flat
    lv.goto(ny("2024-04-25", "10:00"))
    lv.quote("TSLA", "99.99", "100.00", "100.00"); lv.ex.tick()
    (o,) = lv.ex.submit(a.id, {"symbol": "TSLA", "side": "buy", "qty": 10}); assert o.status == "filled"
    state["body"] = json.dumps(ca_body({"split": "2024-04-24"}), default=str)
    lv.goto(ny("2024-04-26", "09:35"))
    assert lv.ex.account(a.id).position("TSLA") == 10


def test_late_dividend_is_paid_on_the_quantity_held_at_the_ex_date_only_to_holders(tmp_path):
    state = {"body": json.dumps(ca_body({}), default=str)}
    lv = Live(ny("2024-04-22", "10:00"), lambda req: (200, state["body"]), data_dir=str(tmp_path))
    held, late = lv.ex.create_account("Holder", "cash"), lv.ex.create_account("Later buyer", "cash")
    lv.quote("AAPL", "99.99", "100.00", "100.00"); lv.ex.tick()
    lv.ex.submit(held.id, {"symbol": "AAPL", "side": "buy", "qty": 100})
    lv.goto(ny("2024-04-22", "11:00"))
    lv.goto(ny("2024-04-24", "09:35"))  # ex-date of the dividend; not published yet
    lv.quote("AAPL", "99.99", "100.00", "100.00"); lv.ex.tick()
    lv.ex.submit(held.id, {"symbol": "AAPL", "side": "buy", "qty": 50})  # bought after the ex-date
    lv.ex.submit(late.id, {"symbol": "AAPL", "side": "buy", "qty": 40})
    lv.ex.save(force=True)  # the snapshots survive a restart
    lv2 = Live(ny("2024-04-24", "12:00"), lambda req: (200, state["body"]), data_dir=str(tmp_path))
    assert lv2.ex.load() and "TSLA" not in str(lv2.ex.lifecycle.held) and "2024-04-24" in lv2.ex.lifecycle.held
    state["body"] = json.dumps(ca_body({"dividend": ("2024-04-24", "2024-04-30")}), default=str)
    lv2.goto(ny("2024-04-25", "09:35"))
    pend = lv2.ex.account(held.id).life.pending_dividends
    assert len(pend) == 1 and pend[0]["qty"] == "100" and pend[0]["amount"] == "25.00"  # 0.25 on 100, not 150
    assert lv2.ex.account(late.id).life.pending_dividends == []


# ---- finding 2: a corporate actions outage has a time limit -------------------------------------------
def test_day_roll_and_margin_checks_stall_forever_on_a_transient_error():
    lv = Live(ny("2024-04-22", "10:00"), (503, {"message": "service unavailable"}))
    a = lv.ex.create_account("Margin", "margin", D("10000"))
    lv.quote("AAPL", "99.99", "100.00", "100.00"); lv.ex.tick()
    (o,) = lv.ex.submit(a.id, {"symbol": "AAPL", "side": "buy", "qty": 150}); assert o.status == "filled"
    lv.goto(ny("2024-04-22", "11:00"))
    for day in ("2024-04-23", "2024-04-24", "2024-04-25", "2024-04-26", "2024-04-29"):
        for hhmm in ("09:35", "10:40", "11:00"):
            lv.goto(ny(day, hhmm))
            lv.quote("AAPL", "29.99", "30.00", "30.00")
            lv.ex.tick()
    life = lv.ex.lifecycle
    assert life.last_date == date(2024, 4, 29), "a week of 503s: no day has rolled"
    assert life.waiting is None and life.actions_outage and "503" in life.actions_outage
    s = lv.ex.summary(a.id)
    assert s["margin_call"] is True
    events = lv.ex.drain_events()
    starts = [e for e in events if e.get("kind") == "corporate_actions_late_start"]
    assert len(starts) == 1 and "did not arrive within 60 minutes" in starts[0]["message"]
    # interest accrued for each rolled day
    assert lv.ex.account(a.id).life.accrued_interest > 0


def test_margin_call_is_not_paused_while_the_day_waits_only_liquidation_is():
    lv = Live(ny("2024-04-22", "10:00"), (503, {"message": "service unavailable"}))
    a = lv.ex.create_account("Margin", "margin", D("10000"))
    lv.quote("AAPL", "99.99", "100.00", "100.00"); lv.ex.tick()
    lv.ex.submit(a.id, {"symbol": "AAPL", "side": "buy", "qty": 150})
    lv.goto(ny("2024-04-22", "11:00"))
    lv.goto(ny("2024-04-23", "00:05"))  # the start of the 23rd waits (inside the hour)
    lv.quote("AAPL", "29.99", "30.00", "30.00"); lv.ex.tick()
    assert lv.ex.lifecycle.waiting is not None
    s = lv.ex.summary(a.id)
    assert s["margin_call"] is True and any("waiting for corporate action data" in r for r in s["margin_paused"])
    assert lv.ex.account(a.id).life.margin_call_since is not None


# ---- finding 3: the simulated feed fills as before for model options with a zero bid ------------------
def test_market_sell_of_a_zero_bid_model_option_fills_as_on_main():
    # Values recorded from main (8021d47) with the same script: the walk fills at the 0.01 minimum.
    ex, feed, clock = make_exchange(ABC=100)
    a = ex.create_account("Margin", "margin")
    exp = optchain.expirations(cal.ny_date(ex.now()))[0]
    sym = build("ABC", exp, "C", max(optchain.strikes(100.0)))
    q = ex.quote(sym)
    assert q.bid == 0 and not q.provider_book
    (b,) = ex.submit(a.id, {"symbol": sym, "side": "buy", "qty": 2})
    (s,) = ex.submit(a.id, {"symbol": sym, "side": "sell", "qty": 1})
    (st,) = ex.submit(a.id, {"symbol": sym, "side": "sell", "qty": 1, "type": "stop", "stop_price": "0.05"})
    clock.advance(1); feed.set_quote("ABC", "99.99", "100.00", "100.00"); ex.tick()
    assert (b.status, b.avg_fill_price) == ("filled", D("0.0200"))
    assert (s.status, s.avg_fill_price) == ("filled", D("0.0100"))
    st = ex.engine.orders[st.id]
    assert (st.status, st.avg_fill_price) == ("filled", D("0.0100"))
    assert ex.account(a.id).cash == D("99995.40") and ex.account(a.id).position(sym) == 0
    pv = ex.preview(a.id, {"symbol": sym, "side": "sell", "qty": 1}) if hasattr(ex, "preview") else None
    assert pv is None or not any("no bid" in w.lower() for w in pv.get("warnings", []))


# ---- finding 4: the article in the one-sided message -------------------------------------------------
def test_one_sided_message_says_a_bid_and_an_ask():
    feed = SimFeed()
    ex = Exchange(feed, SimClock(ny("2024-04-22", "10:00")), session_mode="us")
    a = ex.create_account("Margin", "margin")
    feed.set_quote("X", "9.99", "10.00"); ex.tick()
    ex.submit(a.id, {"symbol": "X", "side": "buy", "qty": 1})
    feed.set_quote("X", "0", "10.00", last="10.00", provider_book=True); ex.tick()
    try:
        ex.submit(a.id, {"symbol": "X", "side": "sell", "qty": 1})
        raise AssertionError("not rejected")
    except OrderRejected as e:
        assert "wait for a bid." in str(e) and "an bid" not in str(e)
    feed.set_quote("X", "9.99", "0", last="10.00", provider_book=True); ex.tick()
    try:
        ex.submit(a.id, {"symbol": "X", "side": "buy", "qty": 1})
        raise AssertionError("not rejected")
    except OrderRejected as e:
        assert "wait for an ask." in str(e)


# ---- property: exactly once, and only what was held over the ex-date -----------------------------------
from hypothesis import given, settings as hsettings, strategies as st  # noqa: E402

DAYS = ["2024-04-23", "2024-04-24", "2024-04-25", "2024-04-26", "2024-04-29", "2024-04-30"]


@hsettings(max_examples=25, deadline=None)
@given(held=st.integers(0, 3), after=st.integers(0, 3), publish=st.integers(0, 3), other=st.integers(0, 2))
def test_late_actions_apply_once_to_what_was_held_over_the_ex_date(held, after, publish, other):
    """Split (2-for-1) and dividend (0.25) on 2024-04-24, published `publish` trading days late. The account
    holds 10*held TSLA and AAPL over the ex-date and buys 10*after more of each later; another account buys
    10*other only after the ex-date."""
    state = {"body": json.dumps(ca_body({}), default=str)}
    lv = Live(ny("2024-04-22", "10:00"), lambda req: (200, state["body"]))
    a, b = lv.ex.create_account("A", "cash"), lv.ex.create_account("B", "cash")

    def buy(acct, qty, px):
        if qty:
            for s in ("TSLA", "AAPL"):
                lv.quote(s, D(px) - D("0.01"), px, px)
            lv.ex.tick()
            for s in ("TSLA", "AAPL"):
                (o,) = lv.ex.submit(acct.id, {"symbol": s, "side": "buy", "qty": qty})
                assert o.status == "filled", o.reason

    buy(a, 10 * held, "100.00")
    lv.goto(ny("2024-04-22", "11:00"))
    full = ca_body({"split": "2024-04-24", "dividend": ("2024-04-24", "2024-05-01")})
    for i, day in enumerate(DAYS):
        if i == 1 + publish:
            state["body"] = json.dumps(full, default=str)
        lv.goto(ny(day, "09:35"))
        if i == 1:  # the ex-date
            buy(a, 10 * after, "50.00")
            buy(b, 10 * other, "50.00")
    acct, bb = lv.ex.account(a.id), lv.ex.account(b.id)
    assert acct.position("TSLA") == 20 * held + 10 * after
    assert bb.position("TSLA") == 10 * other
    pend = acct.life.pending_dividends
    assert [p["qty"] for p in pend] == ([str(10 * held)] if held else [])
    assert bb.life.pending_dividends == []
    assert sum(t.kind == "split" for t in acct.ledger.transactions) == (1 if held else 0)
