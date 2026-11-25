"""Buying-power rejections say what was required and what was available."""
import pytest

from app.exchange import OrderRejected
from tests.helpers import make_exchange


def reject(ex, aid, req):
    with pytest.raises(OrderRejected) as err:
        ex.submit(aid, req)
    pre = ex.preview(aid, req)
    assert not pre["ok"] and pre["reason"] == str(err.value)
    return str(err.value)


def test_margin_rejection_has_amounts():
    ex, feed, clock = make_exchange()
    a = ex.create_account("M", "margin", 10000).id
    msg = reject(ex, a, {"symbol": "ABC", "side": "buy", "qty": 300})
    assert msg == "insufficient buying power: this order needs $30,000.00 of buying power and $20,000.00 is available"


def test_stop_rejection_explains_the_stop_price_reservation():
    ex, feed, clock = make_exchange()
    a = ex.create_account("M", "margin", 10000).id
    msg = reject(ex, a, {"symbol": "ABC", "side": "buy", "qty": 150, "type": "stop", "stop_price": "150", "tif": "gtc"})
    # 150 shares reserved at $150 are worth $15,000 at today's $100: the $7,500 gap counts against equity too
    assert msg.startswith("insufficient buying power: this order needs $30,000.00 of buying power and $20,000.00 is available")
    assert "reserves buying power as if it fills at its stop price ($150.00)" in msg


def test_bracket_rejection_has_amounts():
    ex, feed, clock = make_exchange()
    a = ex.create_account("M", "margin", 10000).id
    msg = reject(ex, a, {"symbol": "ABC", "side": "buy", "qty": 250, "type": "limit", "limit_price": "100",
                         "take_profit": "120", "stop_loss": "90"})
    assert msg == "insufficient buying power: this order needs $25,000.00 of buying power and $20,000.00 is available"


def test_open_orders_reserve_buying_power_and_the_message_says_so():
    ex, feed, clock = make_exchange()
    a = ex.create_account("M", "margin", 10000).id
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100, "type": "limit", "limit_price": "90", "tif": "gtc"})
    msg = reject(ex, a, {"symbol": "ABC", "side": "buy", "qty": 150})
    # the resting buy is reserved at its limit ($9,000 of cash for $10,000 of stock at today's price)
    assert msg == ("insufficient buying power: this order needs $15,000.00 of buying power and $12,000.00 is available "
                   "($8,000.00 is reserved by your other open orders)")


def test_cash_account_rejection_has_amounts():
    ex, feed, clock = make_exchange()
    a = ex.create_account("C", "cash", 5000).id
    msg = reject(ex, a, {"symbol": "ABC", "side": "buy", "qty": 60})
    assert msg == "insufficient cash: this order needs $6,000.00 of cash and $5,000.00 is available"


def test_replace_rejection_has_amounts():
    ex, feed, clock = make_exchange()
    a = ex.create_account("M", "margin", 10000).id
    o = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100, "type": "limit", "limit_price": "90", "tif": "gtc"})[0]
    with pytest.raises(OrderRejected) as err:
        ex.replace(a, o.id, {"qty": 300})
    assert str(err.value) == ("insufficient buying power for the change: the change needs $16,000.00 of buying power "
                              "and $12,000.00 is available ($8,000.00 is reserved by your open orders, this one at its "
                              "current size)")
    assert ex.engine.orders[o.id].qty == 100


def test_structural_reasons_are_unchanged():
    ex, feed, clock = make_exchange()
    a = ex.create_account("C", "cash", 5000).id
    assert reject(ex, a, {"symbol": "ABC", "side": "sell", "qty": 1}) == "short selling needs a margin account"
