"""Hypothesis invariants for the lifecycle rules."""
import json
from datetime import date, timedelta
from decimal import Decimal

from hypothesis import HealthCheck, given, settings, strategies as st

from app.core.models import Fill
from app.exchange import OrderRejected
from app.ledger.account import Account
from app.lifecycle import settings as cfg
from app.lifecycle import splits
from app.lifecycle.actions import dividend, split
from app.lifecycle.manager import uncovered
from app.lifecycle.state import AccountLife
from app.options import chain
from app.options.symbology import build, parse
from app.risk import margin
from helpers import D, make_exchange, move

EXPIRY = chain.expirations(date(2026, 11, 24))[-1]
# Ratios as (new, old). 2-for-3 style splits are left out: with them the adjusted price can sit
# exactly on a half cent, where rounding the account total can differ by a cent.
RATIOS = [(2, 1), (3, 1), (4, 1), (10, 1), (3, 2), (5, 2), (1, 2), (1, 3), (1, 5), (1, 10), (1, 20), (3, 4)]

cents_st = st.integers(1, 100000).map(lambda n: Decimal(n) / 100)


def ledger_ok(a):
    assert all(sum(v for _, v in t.postings) == 0 for t in a.ledger.transactions)
    assert sum(a.ledger.balances.values()) == 0
    assert {k: v for k, v in a.ledger.recompute().items() if v} == {k: v for k, v in a.ledger.balances.items() if v}
    for sym in set(a.lots) | {k[4:] for k in a.ledger.balances if k.startswith("pos:")}:
        assert a.ledger.balance(f"pos:{sym}") == a.cost_basis(sym)


def _fill(i, side, qty, price, symbol="ABC", fee="0"):
    return Fill(id=f"F{i}", order_id=None, account_id="A1", symbol=symbol, side=side, qty=qty, price=price,
                ts=float(i), fee=D(fee))


@settings(max_examples=400, deadline=None)
@given(fills=st.lists(st.tuples(st.sampled_from(["buy", "sell"]), st.integers(1, 400), cents_st,
                                st.integers(0, 200)), min_size=1, max_size=12),
       ratio=st.sampled_from(RATIOS), price=cents_st, other=st.integers(-50, 50),
       option=st.tuples(st.integers(-5, 5), st.sampled_from(["C", "P"]), st.sampled_from(["50", "100", "105", "33.5"]),
                        cents_st))
def test_split_conserves_account_value_and_keeps_the_ledger_balanced(fills, ratio, price, other, option):
    a = Account("A1", "t", "margin", D("1000000"), 0)
    for i, (side, qty, px, fee) in enumerate(fills, 1):
        a.apply_fill(_fill(i, side, Decimal(qty), px, fee=str(Decimal(fee) / 100)), 1)
    if other:
        a.apply_fill(_fill(90, "buy" if other > 0 else "sell", Decimal(abs(other)), D("12.34"), "XYZ"), 1)
    n_opt, kind, strike, opt_mark = option
    opt = build("ABC", EXPIRY, kind, D(strike))
    if n_opt:
        a.apply_fill(_fill(91, "buy" if n_opt > 0 else "sell", Decimal(abs(n_opt)), opt_mark, opt), 100)
    marks = {"ABC": price, "XYZ": D("12.34"), opt: opt_mark}
    before_pos = a.positions()
    before = margin.equity(a.cash, before_pos, marks)
    basis_before = a.cost_basis("ABC")
    new, old = ratio
    act = split("ABC", date(2026, 11, 25), new, old)
    ids = iter(f"L{i}" for i in range(100))
    res = splits.split_stock(a, act, price, 100.0)
    notes = splits.split_options(a, act, {opt: opt_mark}, 100.0, lambda: next(ids))

    post_marks = {"ABC": price * old / new, "XYZ": D("12.34")}
    for note in notes:
        if note["kind"] == "adjusted":
            post_marks[note["new_symbol"]] = opt_mark / act.whole_forward_factor
    after = margin.equity(a.cash, a.positions(), post_marks)
    assert after == before  # account value is unchanged to the cent
    ledger_ok(a)
    if res:
        q = before_pos["ABC"]
        exact = abs(q) * new / old
        assert a.position("ABC") == (1 if q > 0 else -1) * exact.to_integral_value(rounding="ROUND_FLOOR")
        if res["fraction"] == 0:
            assert a.cost_basis("ABC") == basis_before  # basis unchanged to the cent
        assert all(l.qty != 0 for l in a.lots.get("ABC", []))
    assert a.position("XYZ") == other


@settings(max_examples=150, deadline=None)
@given(fills=st.lists(st.tuples(st.sampled_from(["buy", "sell"]), st.integers(1, 10000).map(lambda n: Decimal(n) / 1000),
                                cents_st), min_size=1, max_size=8),
       ratio=st.sampled_from(RATIOS), price=cents_st)
def test_split_of_fractional_positions_moves_value_by_at_most_a_cent(fills, ratio, price):
    a = Account("A1", "t", "margin", D("1000000"), 0)
    for i, (side, qty, px) in enumerate(fills, 1):
        a.apply_fill(_fill(i, side, qty, px), 1)
    before = margin.equity(a.cash, a.positions(), {"ABC": price})
    new, old = ratio
    splits.split_stock(a, split("ABC", date(2026, 11, 25), new, old), price, 100.0)
    after = margin.equity(a.cash, a.positions(), {"ABC": price * old / new})
    assert abs(after - before) <= D("0.01")
    ledger_ok(a)


ops = st.lists(st.one_of(
    st.tuples(st.just("trade"), st.sampled_from(["buy", "sell"]), st.integers(1, 300)),
    st.tuples(st.just("price"), st.integers(2000, 20000)),
    st.tuples(st.just("day"), st.integers(1, 12)),
    st.tuples(st.just("dividend"), st.integers(1, 300)),
    st.tuples(st.just("split"), st.sampled_from(RATIOS)),
), min_size=1, max_size=25)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops=ops, drip=st.booleans(), kind=st.sampled_from(["margin", "cash"]))
def test_ledger_balances_through_any_lifecycle_sequence(ops, drip, kind):
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", kind, D("50000"))
    a.settings.update(dividend_reinvestment=drip, margin_call_grace_hours="0")
    price = D("100")
    for op in ops:
        if op[0] == "trade":
            try:
                ex.submit(a.id, {"symbol": "ABC", "side": op[1], "qty": op[2]})
            except OrderRejected:
                pass
        elif op[0] == "price":
            price = Decimal(op[1]) / 100
            move(ex, feed, clock, "ABC", price, bid_size=100000, ask_size=100000)
        elif op[0] == "day":
            clock.advance(86400 * op[1])
            ex.tick()
        else:
            tomorrow = ex.lifecycle.today() + timedelta(days=1)
            if op[0] == "dividend":
                feed.schedule(dividend("ABC", tomorrow, tomorrow + timedelta(days=2), Decimal(op[1]) / 100))
            else:
                feed.schedule(split("ABC", tomorrow, *op[1]))
            clock.advance(86400)
            ex.tick()
        ledger_ok(a)
        s = ex.summary(a.id)
        assert s["settled_cash"] + s["unsettled_cash"] == s["cash"] and s["unsettled_cash"] >= 0
    b = Account.from_plain(json.loads(json.dumps(a.to_plain())))
    assert b.cash == a.cash and b.positions() == a.positions() and b.life.to_plain() == a.life.to_plain()
    assert b.settings == a.settings


EXPIRIES = chain.expirations(date(2026, 11, 24))


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(cash=st.integers(-200000, 200000),
       stocks=st.lists(st.tuples(st.sampled_from(["ABC", "XYZ", "QRS"]), st.integers(-800, 800).filter(bool),
                                 st.integers(500, 30000)), min_size=1, max_size=4),
       options=st.lists(st.tuples(st.sampled_from(["ABC", "XYZ"]), st.sampled_from("CP"),
                                  st.sampled_from([-10, -5, 0, 5, 10]), st.integers(1, len(EXPIRIES) - 1),
                                  st.integers(-5, 5).filter(bool)), max_size=4))
def test_liquidation_never_increases_the_requirement_or_uncovers_a_short(cash, stocks, options):
    prices = {"ABC": D("60"), "XYZ": D("25"), "QRS": D("140")}
    ex, feed, clock = make_exchange(**{s: p for s, p in prices.items()})
    for s, p in prices.items():
        feed.set_quote(s, p - D("0.01"), p, p, bid_size=100000, ask_size=100000)
    a = ex.create_account("t", "margin", D("1000"))
    a.settings.update(margin_call_grace_hours="720")
    a.ledger.post(0, "deposit", "test", [("cash", D(cash)), ("equity:capital", -D(cash))])
    for i, (sym, qty, entry) in enumerate(stocks):
        a.apply_fill(_fill(i, "buy" if qty > 0 else "sell", Decimal(abs(qty)), Decimal(entry) / 100, sym), 1)
    for i, (u, kind, off, e, qty) in enumerate(options):
        osym = build(u, EXPIRIES[e], kind, prices[u] + off)
        mark = ex.mark(osym)
        if mark > 0:
            a.apply_fill(_fill(60 + i, "buy" if qty > 0 else "sell", Decimal(abs(qty)), mark, osym), 100)
    ex.tick()
    before = uncovered(a.positions())
    steps = ex.lifecycle.liquidate(a, clock.now())
    for st_ in steps:
        assert st_["requirement_after"] <= st_["requirement_before"]
        assert st_["status"] == "filled"
        # a short option that was covered before liquidation is never left uncovered
        assert all(v <= before.get(k, 0) for k, v in uncovered(st_["positions"]).items())
    pos = a.positions()
    marks = ex._marks(pos)
    stuck = all((ex.quote(s).bid if q > 0 else ex.quote(s).ask) <= 0 for s, q in pos.items())
    assert not pos or stuck or margin.equity(a.cash, pos, marks) >= margin.requirement(pos, marks, maintenance=True)
    ledger_ok(a)


life_st = st.builds(
    AccountLife,
    unsettled=st.lists(st.tuples(st.dates().map(date.isoformat), cents_st, st.text(max_size=5)).map(list), max_size=3),
    day_trades=st.lists(st.tuples(st.dates().map(date.isoformat), st.sampled_from(["ABC", "XYZ"]),
                                  st.text(max_size=5)).map(list), max_size=4),
    opened_date=st.one_of(st.none(), st.dates().map(date.isoformat)),
    opened=st.dictionaries(st.sampled_from(["ABC", "XYZ"]), st.lists(
        st.tuples(st.one_of(st.none(), st.text(max_size=5)), cents_st, st.booleans()).map(list), max_size=3)),
    liquidation_paused=st.booleans(),
    log=st.lists(st.tuples(st.floats(0, 2e9), st.sampled_from(["margin_call", "liquidation"]),
                           st.text(max_size=20)).map(list), max_size=3),
    accrued_interest=st.integers(-10**9, 10**9).map(lambda n: Decimal(n) / 10**10),
    accrued_borrow=cents_st, margin_call_since=st.one_of(st.none(), st.floats(0, 2e9)),
)


@settings(max_examples=200, deadline=None)
@given(life=life_st, rate=st.integers(0, 5000).map(lambda n: str(Decimal(n) / 100)), flags=st.lists(st.booleans(), min_size=3, max_size=3))
def test_account_lifecycle_state_round_trips_through_json(life, rate, flags):
    a = Account("A1", "t", "margin", D("1000"), 0)
    a.life = life
    a.settings = cfg.update(a.settings, {"margin_interest_rate": rate, "dividend_reinvestment": flags[0],
                                         "pattern_day_trader": flags[1], "settlement": flags[2]})
    b = Account.from_plain(json.loads(json.dumps(a.to_plain())))
    assert b.life == a.life and b.settings == a.settings
