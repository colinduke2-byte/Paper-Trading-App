"""Minimal reproductions of failures found by the soak harness (tools/soak).

Each test states the correct behaviour and is marked xfail(strict=True) with the reason, so the
suite stays green while the bug exists and the fix turns the test into a pass (strict: an
unexpected pass fails the suite, telling whoever fixed it to remove the marker).
"""
import json
import os
from datetime import date, timedelta

import pytest

from app.analytics.common import ny_date
from app.analytics.statement import monthly_statement
from app.core.models import Fill
from app.lifecycle.actions import dividend, split
from helpers import D, T0, make_exchange, move


def utc_day(ts, days=0):
    return date(1970, 1, 1) + timedelta(days=int(ts // 86400) + days)


def statement_now(ex, aid):
    a = ex.account(aid)
    d = ny_date(ex.now())
    return monthly_statement(a, d.year, d.month, ex.now(), ex.summary(aid)["equity"])


def next_day(ex, feed, clock, price="100.00"):
    move(ex, feed, clock, "ABC", price, seconds=86400)


# ---- statements --------------------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason=(
    "analytics/statement.py monthly_statement: ledger kinds outside TRADE_KINDS (reinvestment, split cash in "
    "lieu, split_settlement, 'closed at expiry') are listed under 'other' with their full cash amount AND "
    "their realised profit is added again, so reconciliation_difference is off by the cash amount"))
@pytest.mark.parametrize("case", ["dividend_reinvestment", "cash_in_lieu", "closed_at_expiry"])
def test_statement_reconciles_with_non_trade_fills(case):
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "cash" if case == "closed_at_expiry" else "margin", D("1000"))
    if case == "dividend_reinvestment":
        a.settings["dividend_reinvestment"] = True
        ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 5})
        day = utc_day(ex.now(), 1)
        feed.schedule(dividend("ABC", day, day, "1.00"))
    elif case == "cash_in_lieu":
        ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 3})
        feed.schedule(split("ABC", utc_day(ex.now(), 1), 3, 2))  # 4.5 shares: half a share in cash
    else:
        # a cash account holding a call it cannot afford to exercise: closed at expiry for its value
        from app.options import chain
        from app.core import calendar as cal
        from app.options.symbology import build
        exp = chain.expirations(cal.ny_date(T0))[0]
        sym = build("ABC", exp, "C", D("50"))
        fill = Fill(id="X1", order_id=None, account_id=a.id, symbol=sym, side="buy", qty=D(1), price=D("50"), ts=ex.now())
        a.apply_fill(fill, 100)  # bought for 5,000 earlier; cash stays above zero for the check below
        a.ledger.post(ex.now(), "deposit", "test top-up", [("cash", D("5000")), ("equity:capital", D("-5000"))])
        a.starting_cash += D("5000")
        clock.set(cal.close_ts(exp) + 60)
        feed.set_quote("ABC", D("99.99"), D("100"), D("100"))
        ex.tick()
        assert any(f.kind == "closed at expiry" for f in a.fills)
    if case != "closed_at_expiry":
        next_day(ex, feed, clock)
    assert ny_date(ex.now()).month == ny_date(T0).month  # one statement month
    st = statement_now(ex, a.id)
    assert st["reconciliation_difference"] == 0, st["other_by_kind"]


@pytest.mark.xfail(strict=True, reason=(
    "analytics/statement.py _positions_at rebuilds holdings from fills only, so a stock split (lots change "
    "without a fill) leaves the statement showing the pre-split quantity and a market value off by the ratio"))
def test_statement_positions_follow_a_split():
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "margin", D("10000"))
    ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 10})
    feed.schedule(split("ABC", utc_day(ex.now(), 1), 2, 1))
    next_day(ex, feed, clock, "50.00")
    assert a.positions() == {"ABC": D(20)}
    st = statement_now(ex, a.id)
    assert {p["symbol"]: p["qty"] for p in st["positions"]} == {"ABC": D(20)}


# ---- cash account goes negative -----------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason=(
    "exchange.py submit/_worst_case: a resting order is only risk-checked when it is entered; a cash "
    "account's buy stop (or market order waiting for the open) that fills after the price gaps up is booked "
    "in full, so a cash account borrows (cash below zero) with nothing to stop it"))
def test_cash_account_cash_never_goes_negative_after_a_gap():
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "cash", D("10600"))
    [o] = ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 100, "type": "stop", "stop_price": "105",
                           "tif": "gtc"})
    assert o.status == "working"
    move(ex, feed, clock, "ABC", "200.00")  # the price gaps through the stop
    assert a.cash >= 0, f"cash account cash {a.cash}, order {o.status}"


# ---- a waiting multi-leg market order whose sold leg loses its bid --------------------------------------
def _waiting_combo():
    from datetime import datetime, timezone
    from app.core import calendar as cal
    from app.core.clock import SimClock
    from app.exchange import Exchange
    from app.marketdata.feed import SimFeed
    from app.options import chain
    from app.options.symbology import build
    t = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc).timestamp()  # a Saturday: the market is closed
    clock, feed = SimClock(t), SimFeed()
    ex = Exchange(feed, clock, session_mode="us")
    feed.set_quote("ABC", D("99.99"), D("100"), D("100"))
    ex.tick()
    a = ex.create_account("m", "margin", D("100000"))
    exp = chain.expirations(cal.ny_date(t))[3]
    legs = [{"symbol": build("ABC", exp, "C", D("100")), "side": "buy"},
            {"symbol": build("ABC", exp, "C", D("110")), "side": "sell"}]
    [o] = ex.submit(a.id, {"legs": legs, "qty": 1, "tif": "gtc"})  # a market combo waits for the open
    assert o.status == "working"
    clock.advance(60)
    feed.set_quote("ABC", D("39.99"), D("40"), D("40"))  # the sold 110 call is now bid at zero
    return ex, feed, clock, a


@pytest.mark.xfail(strict=True, reason=(
    "exchange.py _worst_case -> _multileg_cost -> _multileg_net raises OrderRejected for a WORKING multi-leg "
    "market order once a sold leg has no bid; summary() calls _worst_case, so tick() raises in the equity "
    "sample and in _roll_sessions (before the feed steps): every tick fails, the lifecycle stops at that day "
    "and the market freezes for every account until the order is canceled; GET /api/accounts/{id} answers 422"))
def test_a_waiting_market_combo_without_a_bid_does_not_stop_the_market():
    ex, feed, clock, a = _waiting_combo()
    errors = 0
    for _ in range(3):
        try:
            ex.tick()
        except Exception:
            errors += 1
        clock.advance(86400)
        feed.set_quote("ABC", D("39.99"), D("40"), D("40"))
    assert errors == 0
    ex.summary(a.id)
