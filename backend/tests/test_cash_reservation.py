"""Cash accounts: the pre-trade check is a reservation that holds for every subset of fills.

Each test is a reproduction from the review of PR 28 (findings 1 to 8). Before the fix every one of
them ended with negative cash, short stock, an uncovered call or an unsecured put, or with the wrong
orders canceled.
"""
from decimal import Decimal

import pytest

from app.core.clock import SimClock
from app.exchange import Exchange, OrderRejected
from app.marketdata.feed import SimFeed
from app.options.symbology import parse
from app.risk import margin
from helpers import D, T0, make_exchange, move
from test_multileg import FAR, NEAR, acct, at_expiry, leg, opt

GTC = {"tif": "gtc"}


def lim(s, side, q, px):
    return {"symbol": s, "side": side, "qty": q, "type": "limit", "limit_price": str(px), **GTC}


def stp(s, side, q, px):
    return {"symbol": s, "side": side, "qty": q, "type": "stop", "stop_price": str(px), **GTC}


def mkt(s, side, q):
    return {"symbol": s, "side": side, "qty": q}


P85, P100, P110, C100 = opt("P", 85, FAR), opt("P", 100, FAR), opt("P", 110, FAR), opt("C", 100, FAR)


def sound(ex, a):
    """No negative cash, no short stock, no uncovered call and no unsecured put."""
    acc = ex.account(a)
    pos = {s: n for s, n in acc.positions().items() if n != 0}
    assert acc.cash >= 0, f"negative cash {acc.cash}"
    assert all(n > 0 for s, n in pos.items() if not parse(s)), "short stock in a cash account"
    ev = margin.evaluate("cash", acc.cash, pos, ex._marks(pos))
    assert ev.ok, ev.reason


def unsettled_ok(ex, a):
    """Turn off the settled-cash rule, which would otherwise reject some of these orders first."""
    ex.account(a).settings.update(settled_cash_only=False)
    return a


def try_submit(ex, a, req):
    try:
        return ex.submit(a, req)
    except OrderRejected:
        return None


# ---- 1. a buy-back of a short put is never free -----------------------------------------------------
def test_resting_buy_larger_than_the_short_put_does_not_free_its_cash():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "10100")
    ex.submit(a, mkt(P100, "sell", 1))
    [bid] = ex.submit(a, lim(P100, "buy", 2, "0.05"))
    with pytest.raises(OrderRejected, match="insufficient cash"):
        ex.submit(a, mkt("ABC", "buy", 100))
    assert bid.status == "working"
    sound(ex, a)


def test_several_resting_buy_backs_of_one_short_put_cannot_all_be_paid_for():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "11001")
    ex.submit(a, mkt(P110, "sell", 1))
    move(ex, feed, clock, "ABC", "80")
    accepted = [o for o in (try_submit(ex, a, lim(P110, "buy", 1, "29.00")) for _ in range(6)) if o]
    assert 1 <= len(accepted) < 6
    move(ex, feed, clock, "ABC", "81.50")
    sound(ex, a)


def test_one_marketable_buy_larger_than_the_short_put_pays_for_every_contract():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "11001")
    ex.submit(a, mkt(P110, "sell", 1))
    move(ex, feed, clock, "ABC", "62")
    with pytest.raises(OrderRejected, match="insufficient cash"):
        ex.submit(a, mkt(P110, "buy", 3))
    sound(ex, a)


def test_raising_a_resting_buy_back_goes_through_the_check():
    ex, feed, clock = make_exchange()
    a = unsettled_ok(ex, acct(ex, "cash", "11001"))
    ex.submit(a, mkt(P110, "sell", 1))
    move(ex, feed, clock, "ABC", "80")
    [bid] = ex.submit(a, lim(P110, "buy", 1, "20.00"))
    spare = ex.summary(a)["buying_power"]  # what the bid at 20.00 leaves
    assert D("0") < spare < D("2000")
    with pytest.raises(OrderRejected, match="insufficient"):  # 6 at 29.00: 17,400, the short frees 11,000
        ex.replace(a, bid.id, {"qty": "6", "limit_price": "29"})
    assert bid.qty == 1 and bid.limit_price == D("20.00")
    ex.replace(a, bid.id, {"qty": "1", "limit_price": "29"})  # a pure buy-back: what it frees pays for it
    move(ex, feed, clock, "ABC", "81.50")
    sound(ex, a)


def test_a_pure_buy_back_of_a_short_put_is_still_accepted_and_kept():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "11001")
    ex.submit(a, mkt(P110, "sell", 1))
    [bid] = ex.submit(a, lim(P110, "buy", 1, "2.00"))  # the 11,000 it frees pays for it
    move(ex, feed, clock, "ABC", "101")
    assert bid.status == "working"
    move(ex, feed, clock, "ABC", "150")
    assert bid.status == "filled" and not ex.positions(a)
    sound(ex, a)


# ---- 2. the premium of a write that has not filled pays for nothing else -----------------------------
def test_resting_put_write_premium_does_not_secure_other_puts():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "1000")
    ex.submit(a, lim(P85, "sell", 1, "300"))  # rests for ever
    with pytest.raises(OrderRejected, match="insufficient cash"):
        ex.submit(a, mkt(P100, "sell", 2))
    at_expiry(ex, feed, clock, "60", FAR)
    sound(ex, a)


def test_resting_covered_call_write_premium_does_not_secure_a_put():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "10100")
    ex.submit(a, mkt("ABC", "buy", 100))  # cash about 100
    ex.submit(a, lim(opt("C", 110, FAR), "sell", 1, "250"))
    with pytest.raises(OrderRejected, match="insufficient cash"):
        ex.submit(a, mkt(P100, "sell", 1))
    sound(ex, a)


def test_a_put_write_still_counts_its_own_premium():
    ex, feed, clock = make_exchange()
    premium = ex.quote(P100).bid * 100
    a = acct(ex, "cash", str(D("10000") - premium + D("0.65")))
    [o] = ex.submit(a, mkt(P100, "sell", 1))
    assert o.status == "filled" and ex.account(a).cash == D("10000")
    sound(ex, a)


# ---- 3. a one-cancels-other group costs its most expensive member ------------------------------------
def test_oco_group_is_priced_at_its_most_expensive_member():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "500")
    with pytest.raises(OrderRejected, match="insufficient cash"):
        ex.submit(a, {"oco": [lim("ABC", "buy", 100, "1.00"), stp("ABC", "buy", 100, "100.50")]})
    move(ex, feed, clock, "ABC", "101")
    sound(ex, a)


def test_bracket_on_a_closing_sale_must_pay_for_its_buy_back():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "11000")
    ex.submit(a, mkt("ABC", "buy", 100))  # cash 1,000
    # the sale closes the holding; its stop-loss exit would buy 100 back at 300 with 15,000 of proceeds
    with pytest.raises(OrderRejected, match="insufficient cash"):
        ex.submit(a, {**lim("ABC", "sell", 100, "150"), "take_profit": "90", "stop_loss": "300"})
    # within what the sale brings in plus the cash there is, it is accepted
    entry, tp, sl = ex.submit(a, {**lim("ABC", "sell", 100, "150"), "take_profit": "90", "stop_loss": "155"})
    assert entry.status == "working" and tp.status == sl.status == "held"
    move(ex, feed, clock, "ABC", "151")
    move(ex, feed, clock, "ABC", "160")
    sound(ex, a)


# ---- 4. a one-triggers-other parent helps only its own children ---------------------------------------
def test_oto_parent_sale_proceeds_pay_for_nothing_but_its_children():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "11000")
    ex.submit(a, mkt("ABC", "buy", 100))  # cash 1,000
    ex.submit(a, {**lim("ABC", "sell", 100, "150"), "triggers": [lim("ABC", "buy", 1, "50")]})
    with pytest.raises(OrderRejected, match="insufficient cash"):
        ex.submit(a, lim("ABC", "buy", 100, "99"))
    move(ex, feed, clock, "ABC", "98")
    sound(ex, a)


def test_oto_parent_buy_shares_are_not_there_for_other_sales():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "20000")
    ex.submit(a, {**lim("ABC", "buy", 100, "50"), "triggers": [lim("ABC", "sell", 1, "500")]})
    with pytest.raises(OrderRejected, match="short selling"):
        ex.submit(a, mkt("ABC", "sell", 10))
    assert not ex.positions(a)
    sound(ex, a)


def test_oto_proceeds_may_pay_for_the_triggered_order():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "10000")
    ex.submit(a, mkt("ABC", "buy", 100))  # cash 0
    entry, child = ex.submit(a, {**mkt("ABC", "sell", 100), "triggers": [lim("ABC", "buy", 50, "99")]})
    assert entry.status == "filled" and child.status == "working"
    sound(ex, a)


# ---- 5. closing orders the account cannot pay for, or that uncover a call ------------------------------
def test_buy_stop_on_a_covered_call_is_canceled_when_it_gaps_out_of_reach():
    ex, feed, clock = make_exchange()
    a = unsettled_ok(ex, acct(ex, "cash", "10300"))
    ex.submit(a, mkt("ABC", "buy", 100))
    ex.submit(a, mkt(C100, "sell", 1))
    [stop] = ex.submit(a, stp(C100, "buy", 1, "4.00"))
    move(ex, feed, clock, "ABC", "125")
    assert stop.status == "canceled" and stop.filled_qty == 0
    assert stop.reason == "The account can no longer pay for this order at the current price"
    sound(ex, a)


def test_replace_cannot_turn_a_closing_sale_into_one_that_uncovers_a_call():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "20500")
    ex.submit(a, mkt("ABC", "buy", 200))
    ex.submit(a, mkt(C100, "sell", 1))
    [o] = ex.submit(a, lim("ABC", "sell", 100, "150"))
    with pytest.raises(OrderRejected, match="uncovered calls"):
        ex.replace(a, o.id, {"qty": "200", "limit_price": "99"})
    assert o.qty == 100 and o.limit_price == D("150")
    move(ex, feed, clock, "ABC", "100")
    sound(ex, a)


# ---- 6. an order that cannot be priced does not switch the check off ----------------------------------
def test_unpriceable_open_order_does_not_switch_the_check_off():
    clock = SimClock(T0 + 9 * 3600)  # 10:00 New York
    feed = SimFeed()
    ex = Exchange(feed, clock, session_mode="us")
    feed.set_quote("ABC", "99.99", "100", "100")
    ex.tick()
    a = acct(ex, "cash", "10400")
    P, C = opt("P", 95, NEAR), opt("C", 110, FAR)
    ex.submit(a, mkt(P, "buy", 1))
    clock.advance(19 * 3600)  # pre-market
    feed.set_quote("ABC", "99.99", "100", "100")
    ex.tick()
    ex.submit(a, {"legs": [leg(P, "sell"), leg(C, "buy")], "qty": 1, "tif": "gtc"})  # waits for the open
    [stop] = ex.submit(a, stp("ABC", "buy", 100, "101"))
    clock.advance(5 * 3600)
    feed.set_quote("ABC", "129.99", "130", "130")
    ex.tick()  # must not raise
    assert stop.status == "canceled" and stop.filled_qty == 0
    ex.summary(a)
    sound(ex, a)


# ---- 7. three or more resting multi-leg orders ----------------------------------------------------------
def test_credit_order_that_does_not_fill_cannot_pay_for_two_debits():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "10430")
    P90, C105, C110 = opt("P", 90, FAR), opt("C", 105, FAR), opt("C", 110, FAR)
    ex.submit(a, mkt("ABC", "buy", 100))
    ex.submit(a, mkt(P90, "buy", 1))
    debit = {"legs": [leg(C105, "buy"), leg(C110, "buy")], "qty": 2, "type": "limit", "limit_price": "1.45",
             "price_effect": "debit", **GTC}
    credit = {"legs": [leg("ABC", "sell", 100), leg(P90, "sell")], "qty": 1, "type": "limit", "limit_price": "120",
              "price_effect": "credit", **GTC}
    ex.submit(a, debit)
    ex.submit(a, credit)
    with pytest.raises(OrderRejected, match="insufficient cash"):
        ex.submit(a, debit)
    move(ex, feed, clock, "ABC", "99.5")
    sound(ex, a)


# ---- 8. the per-tick check cancels only what it must, with the right reason -------------------------------
def test_tick_check_cancels_only_the_order_that_causes_the_shortfall():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "10400")
    [stop] = ex.submit(a, stp("ABC", "buy", 100, "101"))
    clock.advance(1)
    small = [ex.submit(a, lim("ABC", "buy", 1, px))[0] for px in (50, 51, 52)]
    move(ex, feed, clock, "ABC", "120")
    assert stop.status == "canceled"
    assert [o.status for o in small] == ["working"] * 3


def test_tick_check_cancels_a_put_write_with_a_reason_that_fits_a_sale():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "9850")
    [put] = ex.submit(a, stp(P100, "sell", 1, "2.00"))
    clock.advance(1)
    buys = [ex.submit(a, lim("ABC", "buy", 1, "10"))[0] for _ in range(3)]
    move(ex, feed, clock, "ABC", "110")
    assert put.status == "canceled" and "pay for" not in put.reason
    assert [o.status for o in buys] == ["working"] * 3
    sound(ex, a)


def test_tick_check_waits_while_nothing_can_trade():
    clock = SimClock(T0 + 9 * 3600)  # 10:00 New York
    feed = SimFeed()
    ex = Exchange(feed, clock, session_mode="us")
    feed.set_quote("ABC", "99.99", "100", "100")
    ex.tick()
    a = acct(ex, "cash", "10200")
    [stop] = ex.submit(a, stp("ABC", "buy", 100, "101"))
    clock.advance(7 * 3600)  # 17:00, after hours: a stop cannot trade
    feed.set_quote("ABC", "119.99", "120", "120")
    ex.tick()
    assert stop.status == "working"
    clock.advance(17 * 3600 - 1800)  # 09:30 next day, the open: now it is canceled before it can fill
    ex.tick()
    assert stop.status == "canceled" and stop.filled_qty == 0
    sound(ex, a)


def test_reason_names_expiry_only_when_an_option_settled():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "10200")
    [stop] = ex.submit(a, stp("ABC", "buy", 100, "101"))
    move(ex, feed, clock, "ABC", "120")
    assert stop.reason == "The account can no longer pay for this order at the current price"
