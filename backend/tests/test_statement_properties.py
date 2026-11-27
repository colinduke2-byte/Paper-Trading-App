"""Statements reconcile to the cent and show the account's real positions, whatever happened in the month:
dividends, reinvestment, splits with cash in lieu, exercise, assignment, expiry, "closed at expiry",
margin interest, borrow fees and fees (release-candidate review N7)."""
from datetime import timedelta
from decimal import Decimal

from hypothesis import HealthCheck, given, settings, strategies as st

from app.analytics.common import months_between, parse_month
from app.analytics.statement import monthly_statement, positions_at
from app.core import calendar as cal
from app.exchange import OrderRejected
from app.lifecycle.actions import CorporateAction, dividend, split
from app.options import chain
from app.options.symbology import build
from helpers import D, T0, make_exchange, move

RATIOS = [(2, 1), (3, 1), (3, 2), (5, 2), (1, 2), (1, 3), (3, 4)]
EXPIRY = chain.expirations(cal.ny_date(T0))[0]

ops = st.lists(st.one_of(
    st.tuples(st.just("trade"), st.sampled_from(["buy", "sell"]), st.integers(1, 300)),
    st.tuples(st.just("option"), st.sampled_from(["buy", "sell"]), st.sampled_from("CP"),
              st.sampled_from([80, 95, 100, 105, 120]), st.integers(1, 3)),
    st.tuples(st.just("price"), st.integers(2000, 20000)),
    st.tuples(st.just("day"), st.integers(1, 12)),
    st.tuples(st.just("expiry"),),
    st.tuples(st.just("dividend"), st.integers(1, 300)),
    st.tuples(st.just("split"), st.sampled_from(RATIOS)),
), min_size=1, max_size=20)


def _statement(ex, a, month):
    splits = {k: CorporateAction.from_plain(r) for k, r in ex.lifecycle.split_records.items()}
    year, mon = parse_month(month)
    now = ex.now()
    held = positions_at(a, float("inf"), splits)
    marks = {s: p for s in held if (p := ex.price(s)) is not None}
    s = ex.summary(a.id)
    return monthly_statement(a, year, mon, now, s["equity"], marks, splits)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops=ops, drip=st.booleans(), kind=st.sampled_from(["margin", "cash"]))
def test_statement_reconciles_and_matches_the_positions(ops, drip, kind):
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", kind, D("50000"))
    a.settings.update(dividend_reinvestment=drip, margin_call_liquidation=False)
    for op in ops:
        try:
            if op[0] == "trade":
                ex.submit(a.id, {"symbol": "ABC", "side": op[1], "qty": op[2]})
            elif op[0] == "option" and clock.now() < cal.close_ts(EXPIRY) - 60:
                ex.submit(a.id, {"symbol": build("ABC", EXPIRY, op[2], D(op[3])), "side": op[1], "qty": op[4]})
        except OrderRejected:
            pass
        if op[0] == "price":
            move(ex, feed, clock, "ABC", Decimal(op[1]) / 100, bid_size=100000, ask_size=100000)
        elif op[0] == "day":
            clock.advance(86400 * op[1])
            ex.tick()
        elif op[0] == "expiry":
            clock.set(max(clock.now(), cal.close_ts(EXPIRY) - 30))
            ex.tick()
            clock.advance(60)
            ex.tick()
        elif op[0] in ("dividend", "split"):
            tomorrow = ex.lifecycle.today() + timedelta(days=1)
            feed.schedule(dividend("ABC", tomorrow, tomorrow + timedelta(days=2), Decimal(op[1]) / 100)
                          if op[0] == "dividend" else split("ABC", tomorrow, *op[1]))
            clock.advance(86400)
            ex.tick()
    for month in months_between(a.created_at, ex.now()):
        st_ = _statement(ex, a, month)
        assert st_["reconciliation_difference"] == 0, (month, st_["other_by_kind"])
    current = _statement(ex, a, months_between(a.created_at, ex.now())[0])
    assert {p["symbol"]: p["qty"] for p in current["positions"]} == a.positions()


def test_examples_from_the_review():
    """2-for-1 split, 3-for-2 split with cash in lieu, a reinvested dividend, a cash-account call closed at expiry."""
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "margin", D("100000"))
    a.settings["dividend_reinvestment"] = True
    ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 101})
    t = ex.lifecycle.today()
    feed.schedule(split("ABC", t + timedelta(days=1), 2, 1))
    feed.schedule(split("ABC", t + timedelta(days=2), 3, 2))
    feed.schedule(dividend("ABC", t + timedelta(days=3), t + timedelta(days=4), "1.00"))
    for _ in range(5):
        clock.advance(86400)
        ex.tick()
    month = months_between(a.created_at, ex.now())[0]
    st_ = _statement(ex, a, month)
    assert st_["reconciliation_difference"] == 0
    assert {p["symbol"]: p["qty"] for p in st_["positions"]} == a.positions()
    assert a.position("ABC") > 303  # 101 -> 202 -> 303, plus the reinvested dividend
    assert [o["kind"] for o in st_["other"]] == ["dividend"]

    ex, feed, clock = make_exchange()
    c = ex.create_account("t", "cash", D("2000"))
    sym = build("ABC", EXPIRY, "C", D("95"))
    ex.submit(c.id, {"symbol": sym, "side": "buy", "qty": 1})
    clock.set(cal.close_ts(EXPIRY) + 1)
    feed.set_quote("ABC", "109.99", "110.00", "110.00")
    ex.tick()
    assert [f.kind for f in c.fills] == ["trade", "closed at expiry"]
    st_ = _statement(ex, c, months_between(c.created_at, ex.now())[0])
    assert st_["reconciliation_difference"] == 0 and st_["other"] == []
