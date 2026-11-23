from decimal import Decimal

from hypothesis import given, settings, strategies as st

from app.core.models import Fill
from app.ledger.account import Account
from app.ledger.ledger import Ledger, position_account
from helpers import D

import pytest


def fill(i, side, qty, price, symbol="ABC", fee="0"):
    return Fill(id=f"F{i}", order_id=None, account_id="A1", symbol=symbol, side=side, qty=D(str(qty)),
                price=D(str(price)), ts=float(i), fee=D(fee))


def test_unbalanced_transaction_is_refused():
    with pytest.raises(ValueError):
        Ledger().post(0, "x", "bad", [("cash", D("1")), ("other", D("-2"))])


def test_long_round_trip_fifo():
    a = Account("A1", "t", "margin", D("10000"), 0)
    a.apply_fill(fill(1, "buy", 10, "100"), 1)
    a.apply_fill(fill(2, "buy", 10, "110"), 1)
    assert a.cash == D("7900") and a.position("ABC") == 20 and a.avg_cost("ABC") == D("105")
    pnl = a.apply_fill(fill(3, "sell", 15, "120", fee="1.00"), 1)
    assert pnl == D("250")  # 10 x 20 + 5 x 10
    assert a.cash == D("9699") and a.realized == D("250") and a.fees_paid == D("1.00")
    assert a.position("ABC") == 5 and a.cost_basis("ABC") == D("550")


def test_short_and_flip_through_zero():
    a = Account("A1", "t", "margin", D("10000"), 0)
    a.apply_fill(fill(1, "sell", 10, "50"), 1)
    assert a.cash == D("10500") and a.position("ABC") == -10 and a.cost_basis("ABC") == D("-500")
    pnl = a.apply_fill(fill(2, "buy", 25, "40"), 1)   # cover 10, go long 15
    assert pnl == D("100") and a.position("ABC") == 15 and a.cost_basis("ABC") == D("600")
    assert a.cash == D("9500")


def test_option_multiplier():
    a = Account("A1", "t", "margin", D("10000"), 0)
    sym = "ABC261218C00100000"
    a.apply_fill(fill(1, "buy", 2, "3.50", sym, fee="1.30"), 100)
    assert a.cash == D("9298.70")
    assert a.apply_fill(fill(2, "sell", 2, "5.00", sym), 100) == D("300")


fills_strategy = st.lists(st.tuples(st.sampled_from(["buy", "sell"]), st.integers(1, 5000).map(lambda n: Decimal(n) / 10),
                                    st.integers(1, 5000000).map(lambda n: Decimal(n) / 10000),
                                    st.sampled_from(["ABC", "XYZ"]), st.integers(0, 300).map(lambda n: Decimal(n) / 100)),
                          min_size=1, max_size=40)


@settings(max_examples=300, deadline=None)
@given(fills_strategy)
def test_ledger_invariants(seq):
    a = Account("A1", "t", "margin", D("1000000"), 0)
    for i, (side, qty, price, sym, fee) in enumerate(seq, 1):
        a.apply_fill(fill(i, side, qty, price, sym, str(fee)), 1)
        # every transaction balances and the running balances equal a full replay
        assert all(sum(v for _, v in t.postings) == 0 for t in a.ledger.transactions[-1:])
    assert {k: v for k, v in a.ledger.recompute().items() if v} == {k: v for k, v in a.ledger.balances.items() if v}
    assert sum(a.ledger.balances.values()) == 0
    for sym in ("ABC", "XYZ"):
        # the position account always equals the remaining cost basis of the open lots
        assert a.ledger.balance(position_account(sym)) == a.cost_basis(sym)
        if a.position(sym) == 0:
            assert a.ledger.balance(position_account(sym)) == 0
    basis = sum(a.cost_basis(s) for s in ("ABC", "XYZ"))
    assert a.cash == D("1000000") + a.realized - a.fees_paid - basis
    b = Account.from_plain(a.to_plain())
    assert b.cash == a.cash and b.positions() == a.positions() and b.realized == a.realized
