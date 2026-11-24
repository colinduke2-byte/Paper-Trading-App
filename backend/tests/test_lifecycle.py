"""Reference cases for corporate actions, settlement, interest, the PDT rule and margin calls.

Every expected number below is worked out by hand in the comment next to it.
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.core import calendar as cal
from app.core.clock import SimClock
from app.exchange import Exchange, OrderRejected
from app.ledger.account import Account
from app.lifecycle import settings as cfg
from app.lifecycle.actions import CorporateAction, dividend, split
from app.marketdata.feed import SimFeed
from app.options import chain
from app.options.symbology import build
from helpers import D, T0, make_exchange, move

# T0 is Tuesday 2026-11-24 (UTC), so the first day roll is into Wednesday 2026-11-25.
EX, EX2, PAY = date(2026, 11, 25), date(2026, 11, 26), date(2026, 11, 28)


def acct(ex, type="margin", cash="100000", **settings):
    a = ex.create_account("t", type, D(cash))
    a.settings.update(settings)
    return a.id


def deep(ex, feed, clock, price, seconds=1):
    """Move the price with deep displayed size so large market orders fill at one price."""
    move(ex, feed, clock, "ABC", price, seconds=seconds, bid_size=100000, ask_size=100000)


def next_day(ex, clock, days=1):
    clock.advance(86400 * days)
    ex.tick()


def logs(ex, a, kind):
    return [msg for _, k, msg in ex.account(a).life.log if k == kind]


def txns(ex, a, kind):
    return [t for t in ex.account(a).ledger.transactions if t.kind == kind]


def ledger_ok(account):
    assert sum(account.ledger.balances.values()) == 0
    assert {k: v for k, v in account.ledger.recompute().items() if v} == \
           {k: v for k, v in account.ledger.balances.items() if v}
    for sym in account.lots:
        assert account.ledger.balance(f"pos:{sym}") == account.cost_basis(sym)


# ---- records -----------------------------------------------------------------------
def test_corporate_action_records_validate_and_round_trip():
    d = dividend("ABC", EX, PAY, "0.25")
    s = split("ABC", EX, 1, 10)
    assert d.describe() == "0.25 per share dividend of ABC" and s.describe() == "1-for-10 reverse split of ABC"
    assert s.is_reverse and split("ABC", EX, 4).whole_forward_factor == 4 and split("ABC", EX, 3, 2).whole_forward_factor is None
    for x in (d, s):
        assert CorporateAction.from_plain(x.to_plain()) == x
    with pytest.raises(ValueError):
        dividend("ABC", EX, EX, "0")
    with pytest.raises(ValueError):
        split("ABC", EX, 2, 2)
    with pytest.raises(TypeError):
        dividend("ABC", EX, PAY, 0.25)  # floats are refused


# ---- splits ----------------------------------------------------------------------------
def test_forward_split_4_for_1():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})  # 100 at the 100.00 ask
    [resting] = ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 50, "type": "limit", "limit_price": "150", "tif": "gtc"})
    before = ex.summary(a)
    feed.schedule(split("ABC", EX, 4))
    next_day(ex, clock)
    [p] = ex.positions(a)
    assert p["qty"] == 400 and p["avg_cost"] == D("25") and p["cost_basis"] == D("10000.00")
    assert ex.quote("ABC").last == D("25.00") and ex.quote("ABC").bid == D("25.00")  # 99.99 / 4 rounds to 25.00
    assert ex.summary(a)["equity"] == before["equity"] == D("100000.00")
    assert resting.status == "canceled" and resting.reason == "Canceled because of the 4-for-1 stock split of ABC effective 2026-11-25"
    [t] = txns(ex, a, "split")
    assert t.memo == "4-for-1 stock split of ABC effective 2026-11-25: 100 shares became 400" and t.postings == []
    ledger_ok(ex.account(a))


def test_reverse_split_1_for_10_with_cash_in_lieu():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 55})  # 55 x 100 = 5,500
    move(ex, feed, clock, "ABC", "110")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 50})  # 50 x 110 = 5,500
    move(ex, feed, clock, "ABC", "120")
    before = ex.summary(a)
    assert before["cash"] == D("89000.00") and before["equity"] == D("101600.00")  # 89,000 + 105 x 120
    feed.schedule(split("ABC", EX, 1, 10))
    next_day(ex, clock)
    acc = ex.account(a)
    # 105 shares -> 10.5; 10 kept, 0.5 paid in lieu. Cash in lieu = 105 x 120 - 10 x 1,200 = 600.00.
    # The half share comes out of the newest lot (50 -> 5.0 -> 4.5): its basis 5,500 x 0.5 / 5 = 550.00,
    # so the realised gain is 600 - 550 = 50.00 and the basis left is 11,000 - 550 = 10,450.00.
    assert acc.position("ABC") == 10 and [l.qty for l in acc.lots["ABC"]] == [D("5.5"), D("4.5")]
    assert [l.price for l in acc.lots["ABC"]] == [D("1000"), D("1100")]
    assert acc.cost_basis("ABC") == D("10450.00") and acc.realized == D("50.00")
    s = ex.summary(a)
    assert s["cash"] == D("89600.00") and s["equity"] == before["equity"] == D("101600.00")
    [t] = txns(ex, a, "split")
    assert t.memo == ("1-for-10 reverse split of ABC effective 2026-11-25: 105 shares became 10; "
                      "cash in lieu of 0.5 share at 1200")
    assert t.postings == [("cash", D("600.00")), ("pos:ABC", D("-550.00")), ("income:realized", D("-50.00"))]
    assert acc.closed[-1]["kind"] == "cash_in_lieu" and acc.closed[-1]["pnl"] == "50.00"
    ledger_ok(acc)


def test_reverse_split_on_a_short_position_charges_cash_in_lieu():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 15})  # short 15 at the 99.99 bid: proceeds 1,499.85
    move(ex, feed, clock, "ABC", "100")
    before = ex.summary(a)
    feed.schedule(split("ABC", EX, 1, 10))
    next_day(ex, clock)
    acc = ex.account(a)
    # -15 -> -1.5: keep -1, pay for half a share: -15 x 100 - (-1 x 1,000) = -500.00.
    # Basis removed 1,499.85 x 0.5 / 1.5 = 499.95; realised = 499.95 - 500 = -0.05.
    assert acc.position("ABC") == -1 and acc.cost_basis("ABC") == D("-999.90")
    assert acc.realized == D("-0.05") and ex.summary(a)["cash"] == before["cash"] - D("500.00")
    assert ex.summary(a)["equity"] == before["equity"]
    ledger_ok(acc)


def test_fractional_shares_and_3_for_2_split():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    b = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 101})  # 101 x 100
    ex.submit(b, {"symbol": "ABC", "side": "buy", "qty": "2.5"})  # fractional position
    before_a, before_b = ex.summary(a)["equity"], ex.summary(b)["equity"]
    feed.schedule(split("ABC", EX, 3, 2))
    next_day(ex, clock)
    # 101 x 3/2 = 151.5: keep 151; cash in lieu = 10,100 - 151 x 66.666...= 10,100 - 10,066.67 = 33.33
    assert ex.account(a).position("ABC") == 151 and ex.account(a).cash == D("89900") + D("33.33")
    # 2.5 x 3/2 = 3.75 shares: already fractional, nothing paid in lieu
    assert ex.account(b).position("ABC") == D("3.75") and ex.account(b).cash == D("100000") - D("250")
    feed.set_quote("ABC", "66.66", "66.67", "66.67")
    ex.tick()
    assert abs(ex.summary(a)["equity"] - before_a) <= D("0.01") * 151  # only the 1/3-cent price rounding moves
    ledger_ok(ex.account(a))
    ledger_ok(ex.account(b))


def _option(kind, strike, underlying="ABC"):
    exp = chain.expirations(date(2026, 11, 23))[-1]  # a monthly expiry well after the split
    return build(underlying, exp, kind, D(strike)), exp


def test_options_adjust_on_whole_number_forward_split():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    call, exp = _option("C", "105")
    put, _ = _option("P", "95")
    ex.submit(a, {"symbol": call, "side": "buy", "qty": 2})
    ex.submit(a, {"symbol": put, "side": "sell", "qty": 1})
    [resting] = ex.submit(a, {"symbol": call, "side": "sell", "qty": 1, "type": "limit", "limit_price": "90", "tif": "gtc"})
    acc = ex.account(a)
    basis_call, basis_put = acc.cost_basis(call), acc.cost_basis(put)
    before = ex.summary(a)["equity"]
    feed.schedule(split("ABC", EX, 4))
    next_day(ex, clock)
    new_call, new_put = build("ABC", exp, "C", D("26.25")), build("ABC", exp, "P", D("23.75"))
    assert acc.positions() == {new_call: 8, new_put: -4}
    assert acc.cost_basis(new_call) == basis_call and acc.cost_basis(new_put) == basis_put
    assert resting.status == "canceled" and "4-for-1 stock split" in resting.reason
    # The model is homogeneous in (spot, strike), so value only moves by rounding each mark to a cent.
    assert abs(ex.summary(a)["equity"] - before) <= D("0.005") * 100 * 12
    ledger_ok(acc)
    # The adjusted strike is off the listed grid, but the position can still be closed.
    assert ex.submit(a, {"symbol": new_call, "side": "sell", "qty": 8})[0].status == "filled"
    with pytest.raises(OrderRejected, match="strike is not listed"):
        ex.submit(a, {"symbol": build("ABC", exp, "C", D("26.25")), "side": "sell", "qty": 1})


def test_options_cash_settled_when_no_adjusted_contract_exists():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    call, _ = _option("C", "100")
    ex.submit(a, {"symbol": call, "side": "buy", "qty": 3})
    acc = ex.account(a)
    feed.schedule(split("ABC", EX, 1, 2))  # reverse split: no standard adjusted contract
    clock.advance(86400)
    mark = ex.mark(call)  # the model mark at the moment the split takes effect
    cash, before = acc.cash, ex.summary(a)["equity"]
    ex.tick()
    assert not acc.positions()
    assert acc.cash == cash + mark * 300  # 3 contracts x 100 x the pre-split mark
    assert ex.summary(a)["equity"] == before  # exactly: the position was worth 3 x 100 x mark
    assert acc.fills[-1].kind == "split_settlement" and acc.fills[-1].price == mark
    ledger_ok(acc)
    # 3-for-1 on a 100 strike would need a 33.333... strike: also cash settled
    b = acct(ex)
    ex.submit(b, {"symbol": "ABC", "side": "buy", "qty": 1})
    _, exp = _option("C", "50")
    c50 = build("ABC", exp, "C", D("50"))
    ex.submit(b, {"symbol": c50, "side": "buy", "qty": 1})
    feed.schedule(split("ABC", EX2, 3))
    next_day(ex, clock)
    assert set(ex.account(b).positions()) == {"ABC"} and ex.account(b).position("ABC") == 3


# ---- dividends ------------------------------------------------------------------------
def test_dividend_long_short_and_reinvestment():
    ex, feed, clock = make_exchange()
    long_, short, drip = acct(ex), acct(ex), acct(ex, dividend_reinvestment=True)
    ex.submit(long_, {"symbol": "ABC", "side": "buy", "qty": 100})
    ex.submit(short, {"symbol": "ABC", "side": "sell", "qty": 50})
    ex.submit(drip, {"symbol": "ABC", "side": "buy", "qty": 100})
    feed.schedule(dividend("ABC", EX, PAY, "0.25"))
    next_day(ex, clock)  # Nov 25: ex-date, holders of record are fixed
    ex.submit(long_, {"symbol": "ABC", "side": "buy", "qty": 100})  # bought on the ex-date: not entitled
    assert ex.summary(long_)["pending_dividends"] == D("25.00") and ex.summary(short)["pending_dividends"] == D("-12.50")
    cash_long, cash_short = ex.account(long_).cash, ex.account(short).cash
    next_day(ex, clock, 2)  # Nov 27: nothing paid yet
    assert ex.account(long_).cash == cash_long and not txns(ex, long_, "dividend")
    move(ex, feed, clock, "ABC", "50")
    next_day(ex, clock)  # Nov 28: pay date
    [t] = txns(ex, long_, "dividend")
    assert t.postings == [("cash", D("25.00")), ("income:dividends", D("-25.00"))]
    assert t.memo == "Dividend: 0.25 per share on 100 ABC (ex-date 2026-11-25)"
    [t] = txns(ex, short, "dividend")
    assert t.postings == [("cash", D("-12.50")), ("expense:short_dividends", D("12.50"))]
    assert ex.account(long_).cash == cash_long + D("25.00") and ex.account(short).cash == cash_short - D("12.50")
    # reinvested: 25.00 / 50.00 = 0.5 share
    d = ex.account(drip)
    assert d.position("ABC") == D("100.5") and d.fills[-1].kind == "reinvestment" and d.cash == D("90000")
    assert ex.summary(long_)["pending_dividends"] == 0
    for x in (long_, short, drip):
        ledger_ok(ex.account(x))


def test_dividends_are_always_paid():
    # Review finding 7: the price always drops on the ex-date, so the "off" switch was removed.
    ex, feed, clock = make_exchange()
    a = acct(ex)
    ex.account(a).settings = cfg.merged({"dividends": False})  # an old saved toggle is ignored
    assert "dividends" not in ex.account(a).settings
    with pytest.raises(ValueError, match="unknown setting dividends"):
        cfg.update(cfg.defaults(), {"dividends": False})
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
    feed.schedule(dividend("ABC", EX, EX2, "1.00"))
    next_day(ex, clock, 2)
    [t] = txns(ex, a, "dividend")
    assert t.postings[0] == ("cash", D("100.00"))


# ---- interest and borrow fees ------------------------------------------------------------------
def test_margin_interest_and_borrow_fee_hand_calculated():
    ex, feed, clock = make_exchange()
    debit = acct(ex, margin_interest_rate="7.2")
    short = acct(ex, borrow_fee_rate="3.6")
    cash = acct(ex, "cash", "200000")
    deep(ex, feed, clock, "100")
    ex.submit(debit, {"symbol": "ABC", "side": "buy", "qty": 1500})  # cash -50,000
    ex.submit(short, {"symbol": "ABC", "side": "sell", "qty": 100})  # short value 100 x 100 = 10,000
    ex.submit(cash, {"symbol": "ABC", "side": "buy", "qty": 1500})
    next_day(ex, clock, 3)  # Nov 24, 25, 26 accrued
    # 50,000 x 7.2% / 360 = 10.00 a day; 10,000 x 3.6% / 360 = 1.00 a day
    assert ex.summary(debit)["accrued_interest"] == D("30.00") and ex.summary(short)["accrued_borrow_fee"] == D("3.00")
    clock.set(datetime(2026, 12, 1, 0, 0, 30, tzinfo=timezone.utc).timestamp())
    ex.tick()  # Nov 24 to Nov 30 = 7 days, posted on Dec 1
    [t] = txns(ex, debit, "interest")
    assert t.postings == [("expense:margin_interest", D("70.00")), ("cash", D("-70.00"))]
    assert t.memo == "Margin interest for November 2026 (7.2% a year, actual/360)"
    [t] = txns(ex, short, "borrow")
    assert t.postings == [("expense:borrow_fees", D("7.00")), ("cash", D("-7.00"))]
    assert not txns(ex, short, "interest") and not txns(ex, cash, "interest")  # no debit; cash accounts never
    assert ex.summary(debit)["accrued_interest"] == 0 and ex.account(debit).cash == D("-50070.00")
    for x in (debit, short):
        ledger_ok(ex.account(x))


def test_default_rates_and_sub_cent_carry():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    deep(ex, feed, clock, "100")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1001})  # debit 100.00
    clock.set(datetime(2026, 12, 1, 0, 0, 30, tzinfo=timezone.utc).timestamp())
    ex.tick()
    # 100 x 8% / 360 = 0.0222... a day; 7 days = 0.1555... -> 0.16 posted, -0.0044... carried
    [t] = txns(ex, a, "interest")
    assert t.postings[0] == ("expense:margin_interest", D("0.16"))
    assert ex.account(a).life.accrued_interest < 0 and ex.summary(a)["accrued_interest"] == D("0.00")


# ---- settlement ------------------------------------------------------------------------------------
def test_t_plus_1_settlement_and_settled_cash_rule():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "10000")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 50})  # 5,000
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 50})  # +4,999.50, settles tomorrow
    s = ex.summary(a)
    assert (s["cash"], s["settled_cash"], s["unsettled_cash"], s["buying_power"]) == \
           (D("9999.50"), D("5000.00"), D("4999.50"), D("5000.00"))
    with pytest.raises(OrderRejected, match="Cash accounts can only buy with settled cash") as err:
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 60})
    assert "need 6,000.00, but settled cash is 5,000.00" in str(err.value) and "2026-11-25" in str(err.value)
    pre = ex.preview(a, {"symbol": "ABC", "side": "buy", "qty": 60})
    assert not pre["ok"] and "settled cash" in pre["reason"]
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 40})  # 4,000 of settled cash is fine
    assert ex.summary(a)["settled_cash"] == D("1000.00")
    next_day(ex, clock)
    s = ex.summary(a)
    assert s["settled_cash"] == s["cash"] == D("5999.50") and s["unsettled_cash"] == 0


def test_settled_cash_rule_can_be_relaxed_and_margin_accounts_are_not_limited():
    ex, feed, clock = make_exchange()
    loose = acct(ex, "cash", "10000", settled_cash_only=False)
    m = acct(ex, "margin", "10000")
    for a in (loose, m):
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 50})
        ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 50})
        assert ex.summary(a)["unsettled_cash"] == D("4999.50")
        assert ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 60})[0].status == "filled"


def test_settlement_skips_holidays_in_us_mode():
    wed = datetime(2026, 11, 25, 10, 0, tzinfo=cal.NY).timestamp()
    clock, feed = SimClock(wed), SimFeed()
    ex = Exchange(feed, clock, session_mode="us")
    feed.set_quote("ABC", "99.99", "100", "100")
    ex.tick()
    a = acct(ex, "cash", "10000")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 10})
    assert ex.account(a).life.unsettled[0][0] == "2026-11-27"  # Thanksgiving (Nov 26) is not a business day
    clock.set(datetime(2026, 11, 26, 12, 0, tzinfo=cal.NY).timestamp())
    ex.tick()
    assert ex.summary(a)["unsettled_cash"] == D("999.90")
    clock.set(datetime(2026, 11, 27, 0, 0, 1, tzinfo=cal.NY).timestamp())
    ex.tick()
    assert ex.summary(a)["unsettled_cash"] == 0


# ---- pattern day trader -----------------------------------------------------------------------------
def _round_trip(ex, a, qty=10):
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": qty})
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": qty})


def test_pattern_day_trader_blocks_opening_orders_after_three_day_trades_under_25000():
    ex, feed, clock = make_exchange()
    a = acct(ex, "margin", "20000")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 50})  # held overnight: closing it is no day trade
    next_day(ex, clock)
    for _ in range(3):
        _round_trip(ex, a)
    assert ex.summary(a)["day_trades"] == 3 and ex.summary(a)["day_trades_left"] == 0
    msg = ("Pattern day trader rule: this account has made 3 day trades in the last 5 business days and its equity "
           "(19,999.70) is below 25,000.00, so it cannot open new positions that could become a 4th day trade. "
           "Closing orders are still allowed. New opening orders are allowed again on 2026-11-30.")
    with pytest.raises(OrderRejected) as err:
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    assert str(err.value) == msg
    with pytest.raises(OrderRejected, match="Pattern day trader rule"):  # a short sale opens too
        ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 60})
    assert not ex.preview(a, {"symbol": "ABC", "side": "buy", "qty": 10})["ok"]
    assert ex.preview(a, {"symbol": "ABC", "side": "sell", "qty": 50})["ok"]
    assert ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 50})[0].status == "filled"  # closing is allowed
    # rolling window: today plus the previous 4 business days (in "always" mode every day counts)
    next_day(ex, clock, 4)
    assert ex.summary(a)["day_trades"] == 3
    next_day(ex, clock, 1)
    assert ex.summary(a)["day_trades"] == 0
    assert ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})[0].status == "filled"


def test_pdt_never_blocks_a_close_during_a_margin_call():
    # Review finding 3: 3 day trades, then a losing position in a margin call must still be closable.
    ex, feed, clock = make_exchange()
    deep(ex, feed, clock, "100")
    a = acct(ex, "margin", "20000")
    for _ in range(2):
        _round_trip(ex, a)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 390})
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 1})  # third day trade
    deep(ex, feed, clock, "60")
    s = ex.summary(a)
    assert s["day_trades"] == 3 and s["margin_call"]
    assert ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 389})[0].status == "filled"
    assert ex.summary(a)["day_trades"] == 3  # the same opening lot: still one day trade


def test_day_trades_count_once_per_opening_lot():
    ex, feed, clock = make_exchange()
    a = acct(ex, "margin", "100000")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 50})
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 50})
    assert ex.summary(a)["day_trades"] == 1  # buy 100, two sells of 50: one day trade
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 20})
    assert ex.summary(a)["day_trades"] == 3  # two opening lots closed the same day


def test_bracket_children_that_close_are_never_blocked():
    ex, feed, clock = make_exchange()
    a = acct(ex, "margin", "20000")
    for _ in range(2):
        _round_trip(ex, a)
    entry, tp, sl = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "take_profit": "101", "stop_loss": "99"})
    _round_trip(ex, a)  # third day trade: opening orders are now blocked
    with pytest.raises(OrderRejected, match="Pattern day trader rule"):
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "take_profit": "101", "stop_loss": "99"})
    move(ex, feed, clock, "ABC", "102")
    assert tp.status == "filled" and ex.account(a).position("ABC") == 0 and ex.summary(a)["day_trades"] == 4


def test_pdt_allows_accounts_over_minimum_and_can_be_disabled():
    ex, feed, clock = make_exchange()
    rich = acct(ex, "margin", "30000")
    off = acct(ex, "margin", "20000", pattern_day_trader=False)
    cash = acct(ex, "cash", "20000")
    for a in (rich, off, cash):
        for _ in range(5):
            _round_trip(ex, a)
    assert ex.summary(rich)["day_trades"] == 5 and ex.summary(rich)["day_trades_left"] is None


def test_pdt_window_uses_trading_days_in_us_mode():
    mon = datetime(2026, 11, 23, 10, 0, tzinfo=cal.NY).timestamp()
    clock, feed = SimClock(mon), SimFeed()
    ex = Exchange(feed, clock, session_mode="us")
    feed.set_quote("ABC", "99.99", "100", "100")
    ex.tick()
    a = acct(ex, "margin", "20000")
    for _ in range(3):
        _round_trip(ex, a)
    # window of 5 trading days from Monday Nov 23: 23, 24, 25, 27 (26 is Thanksgiving), 30
    clock.set(datetime(2026, 11, 30, 10, 0, tzinfo=cal.NY).timestamp())
    ex.tick()
    assert ex.summary(a)["day_trades"] == 3
    clock.set(datetime(2026, 12, 1, 10, 0, tzinfo=cal.NY).timestamp())
    ex.tick()
    assert ex.summary(a)["day_trades"] == 0


def test_partial_fills_of_one_closing_order_are_one_day_trade():
    ex, feed, clock = make_exchange()
    a = acct(ex, "margin", "100000")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 900})
    feed.set_quote("ABC", "99.99", "100", "100", bid_size=500)
    ex.tick()
    [o] = ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 900, "type": "limit", "limit_price": "99.99"})
    move(ex, feed, clock, "ABC", "100", bid_size=500)
    assert len([f for f in ex.account(a).fills if f.order_id == o.id]) == 2
    assert ex.account(a).position("ABC") == 0 and ex.summary(a)["day_trades"] == 1


# ---- margin calls -------------------------------------------------------------------------------------
def test_margin_call_grace_then_liquidation():
    ex, feed, clock = make_exchange()
    deep(ex, feed, clock, "100")
    a = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1900})  # 190,000: cash -90,000
    [resting] = ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 100, "type": "limit", "limit_price": "200", "tif": "gtc"})
    deep(ex, feed, clock, "60")
    # equity 1,900 x 60 - 90,000 = 24,000; maintenance 25% x 114,000 = 28,500
    s = ex.summary(a)
    assert s["margin_call"] and s["margin_call_since"] == clock.now() and s["liquidation_at"] == clock.now() + 48 * 3600
    [t] = logs(ex, a, "margin_call")
    assert t.startswith("Margin call: equity 24,000.00 is below the 28,500.00 maintenance requirement (deficit 4,500.00)")
    assert not txns(ex, a, "margin_call")  # call events are not ledger entries
    clock.advance(47 * 3600)
    ex.tick()
    assert ex.account(a).position("ABC") == 1900 and resting.active
    clock.advance(3600)
    ex.tick()
    # Each share sold cuts the requirement by 15.00 but costs 0.01 of spread: 4,500 / 14.99 -> 301 shares at 59.99.
    # Cash -90,000 + 301 x 59.99 = -71,943.01; equity 1,599 x 60 - 71,943.01 = 23,996.99 >= 23,985.00
    acc = ex.account(a)
    assert resting.status == "canceled" and resting.reason == "Canceled for margin call liquidation"
    assert acc.position("ABC") == 1599 and acc.cash == D("-71943.01")
    s = ex.summary(a)
    assert s["equity"] == D("23996.99") and s["maintenance_requirement"] == D("23985.00") and not s["margin_call"]
    [liq] = logs(ex, a, "liquidation")
    assert liq.startswith("Margin call liquidation: sell 301 ABC at market (order O00000003, filled)")
    order = ex.engine.orders["O00000003"]
    assert order.role == "liquidation" and order.status == "filled"
    # Only 0.05% above maintenance: the call stays open until equity has held for 5 minutes.
    started = s["margin_call_since"]
    assert started is not None
    clock.advance(1)
    ex.tick()
    clock.advance(299)
    ex.tick()
    assert ex.summary(a)["margin_call_since"] == started
    clock.advance(1)
    ex.tick()
    assert ex.summary(a)["margin_call_since"] is None
    assert logs(ex, a, "margin_call")[-1].startswith("Margin call met: equity 23,996.99")
    ledger_ok(acc)


def test_margin_call_met_by_recovery_and_liquidation_can_be_disabled():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    keep = acct(ex, margin_call_liquidation=False)
    deep(ex, feed, clock, "100")
    for x in (a, keep):
        ex.submit(x, {"symbol": "ABC", "side": "buy", "qty": 1900})
    deep(ex, feed, clock, "60")
    deep(ex, feed, clock, "70")  # 1,900 x 70 - 90,000 = 43,000 >= 33,250
    assert logs(ex, a, "margin_call")[-1].startswith("Margin call met: equity 43,000.00")  # 2% above: at once
    deep(ex, feed, clock, "60")
    clock.advance(100 * 3600)
    ex.tick()
    assert ex.account(keep).position("ABC") == 1900 and ex.summary(keep)["liquidation_at"] is None
    assert ex.account(a).position("ABC") < 1900


def test_liquidation_waits_for_the_regular_session_in_us_mode():
    fri = datetime(2026, 11, 20, 15, 0, tzinfo=cal.NY).timestamp()
    clock, feed = SimClock(fri), SimFeed()
    ex = Exchange(feed, clock, session_mode="us")
    deep(ex, feed, clock, "100")
    a = acct(ex, margin_call_grace_hours="1")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1900})
    deep(ex, feed, clock, "60")
    clock.set(datetime(2026, 11, 21, 12, 0, tzinfo=cal.NY).timestamp())  # Saturday: market closed
    ex.tick()
    assert ex.account(a).position("ABC") == 1900
    clock.set(datetime(2026, 11, 23, 9, 30, 5, tzinfo=cal.NY).timestamp())
    ex.tick()
    assert ex.account(a).position("ABC") == 1599


# ---- settings, persistence and API ---------------------------------------------------------------------
def test_settings_validation():
    s = cfg.defaults()
    assert s["margin_interest_rate"] == "8" and s["pdt_min_equity"] == "25000" and s["settled_cash_only"] is True
    assert cfg.update(s, {"margin_interest_rate": 9.5})["margin_interest_rate"] == "9.5"
    for bad, msg in (({"nope": 1}, "unknown setting"), ({"dividend_reinvestment": "yes"}, "true or false"),
                     ({"margin_interest_rate": 80}, "between"), ({"borrow_fee_rate": "abc"}, "number")):
        with pytest.raises(ValueError, match=msg):
            cfg.update(s, bad)
    assert cfg.merged({"dividend_reinvestment": True, "junk": 1, "margin_interest_rate": "bad"}) == \
        dict(cfg.defaults(), dividend_reinvestment=True)
    # Review finding 10: absurd numbers are normalised or refused
    for raw, want in (("-0", "0"), ("1E1", "10"), ("5.00000", "5"), (" 6.123456 ", "6.1235"), ("0.00001", "0")):
        assert cfg.update(s, {"margin_interest_rate": raw})["margin_interest_rate"] == want
    for raw in ("1e-999999", "0.000000000000000000000000001", "1e999999", "Infinity", "NaN", True, None, [1]):
        with pytest.raises(ValueError):
            cfg.update(s, {"margin_interest_rate": raw})


def test_old_state_without_lifecycle_fields_loads():
    a = Account("A1", "t", "margin", D("1000"), 0)
    d = a.to_plain()
    del d["settings"], d["lifecycle"]
    b = Account.from_plain(d)
    assert b.settings == cfg.defaults() and b.life.unsettled == [] and b.life.accrued_interest == 0


def test_lifecycle_state_survives_restart(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = acct(ex, "margin", "20000", margin_interest_rate="7.2")
    c = acct(ex, "cash", "10000")
    for _ in range(2):
        _round_trip(ex, a)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 300})  # debit 10,000
    ex.submit(c, {"symbol": "ABC", "side": "buy", "qty": 10})
    ex.submit(c, {"symbol": "ABC", "side": "sell", "qty": 10})
    feed.schedule(dividend("ABC", EX, PAY, "0.50"))
    next_day(ex, clock)
    move(ex, feed, clock, "ABC", "40")  # 300 x 40 - 10,000 = 2,000 < 3,000: margin call
    c_life = ex.account(c).life.to_plain()
    before = {x: ex.summary(x) for x in (a, c)}
    assert before[a]["margin_call"] and before[a]["pending_dividends"] == D("150.00")
    assert before[a]["accrued_interest"] == D("2.00")  # one day on 10,000 at 7.2% / 360
    ex.save(force=True)

    feed2, clock2 = SimFeed(), SimClock(clock.now())
    ex2 = Exchange(feed2, clock2, data_dir=str(tmp_path))
    assert ex2.load()
    feed2.set_quote("ABC", "39.99", "40", "40")
    ex2.tick()
    assert {x: ex2.summary(x) for x in (a, c)} == before
    assert ex2.account(a).settings["margin_interest_rate"] == "7.2" and ex2.account(c).life.to_plain() == c_life
    assert ex2.lifecycle.last_date == ex.lifecycle.last_date and ex2.lifecycle.applied == {"ABC-div-2026-11-25"}
    feed2.schedule(dividend("ABC", EX, PAY, "0.50"))
    clock2.advance(86400 * 3)
    ex2.tick()  # the dividend is paid once, not recorded twice
    assert [t.postings[0] for t in txns(ex2, a, "dividend")] == [("cash", D("150.00"))]
    fill_ids = [f.id for x in ex2.accounts.values() for f in x.fills]
    assert len(fill_ids) == len(set(fill_ids))


def test_settings_api():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    c = TestClient(create_app(ex, run_loop=False))
    r = c.get(f"/api/accounts/{a}/settings").json()
    assert r["settings"] == cfg.defaults() and {f["key"] for f in r["fields"]} == set(cfg.SPEC)
    r = c.put(f"/api/accounts/{a}/settings", json={"margin_interest_rate": 6.25, "dividend_reinvestment": True})
    assert r.status_code == 200 and r.json()["settings"]["margin_interest_rate"] == "6.25"
    assert ex.account(a).settings["dividend_reinvestment"] is True and ex.dirty
    r = c.put(f"/api/accounts/{a}/settings", json={"borrow_fee_rate": -1})
    assert r.status_code == 400 and "between" in r.json()["detail"]
    assert c.put("/api/accounts/NOPE/settings", json={}).status_code == 404
    assert c.post(f"/api/accounts/{a}/settings/reset").json()["settings"] == cfg.defaults()
    feed.schedule(dividend("ABC", EX2, PAY, "0.25"))
    acts = c.get("/api/corporate-actions", params={"symbol": "abc"}).json()
    assert [x["id"] for x in acts] == ["ABC-div-2026-11-26"] and acts[0]["applied"] is False
    life = c.get(f"/api/accounts/{a}/lifecycle").json()
    assert life["day_trade_count"] == 0 and life["unsettled"] == []
    s = c.get(f"/api/accounts/{a}").json()
    assert {"settled_cash", "day_trades", "accrued_interest", "liquidation_at"} <= set(s)


def test_reset_keeps_settings():
    ex, _, _ = make_exchange()
    a = acct(ex, margin_interest_rate="5")
    ex.reset_account(a)
    assert ex.account(a).settings["margin_interest_rate"] == "5"


# ---- synthetic feed schedule --------------------------------------------------------------------------
def test_synthetic_feed_schedules_quarterly_dividends_and_a_yearly_split():
    from app.marketdata.synthetic import UNIVERSE, SyntheticFeed
    f1, f2 = SyntheticFeed(seed=1, now=T0, history_days=30), SyntheticFeed(seed=2, now=T0, history_days=30)
    year = f1.corporate_actions(date(2027, 1, 1), date(2027, 12, 31))
    assert [x.to_plain() for x in year] == [x.to_plain() for x in f2.corporate_actions(date(2027, 1, 1), date(2027, 12, 31))]
    divs = [x for x in year if x.kind == "dividend"]
    payers = {u[0]: u[7] for u in UNIVERSE if u[7] > 0}
    assert {x.symbol for x in divs} == set(payers) and len(divs) == 4 * len(payers)
    orbt = [x for x in divs if x.symbol == "ORBT"]
    assert all(x.amount == D("0.75") for x in orbt)  # 3.00 a year paid quarterly
    assert all(x.ex_date.weekday() < 5 and timedelta(days=14) <= x.pay_date - x.ex_date <= timedelta(days=16)
               for x in divs)
    us = SyntheticFeed(seed=1, now=T0, history_days=30, always_open=False)
    assert all(cal.is_trading_day(x.ex_date) and cal.is_trading_day(x.pay_date)
               for x in us.corporate_actions(date(2027, 1, 1), date(2027, 12, 31)) if x.kind == "dividend")
    [s] = [x for x in year if x.kind == "split"]
    assert (s.new, s.old, s.ex_date) == (2, 1, date(2027, 9, 15))


def test_synthetic_feed_split_adjusts_history_and_survives_snapshot():
    from app.marketdata.synthetic import SyntheticFeed
    f = SyntheticFeed(seed=1, now=T0, history_days=30)
    q, bar, div = f.quote("STRM"), list(f.store.daily["STRM"][-1]), f.info["STRM"]["dividend"]
    act = split("STRM", EX, 4)
    f.apply_corporate_action(act)
    f.apply_corporate_action(act)  # applied once only
    q2 = f.quote("STRM")
    assert q2.last == (q.last / 4).quantize(D("0.01"))
    assert f.store.daily["STRM"][-1][4] == round(bar[4] / 4, 4) and f.store.daily["STRM"][-1][5] == bar[5] * 4
    f.apply_corporate_action(split("ORBT", EX, 2))
    assert f.info["ORBT"]["dividend"] == 1.5
    g = SyntheticFeed(seed=1, now=T0, history_days=30)
    g.restore(json.loads(json.dumps(f.snapshot())))
    assert g.applied_actions == f.applied_actions and g.info["ORBT"]["dividend"] == 1.5 and g.quote("STRM").last == q2.last
    assert div == 0.0


def test_synthetic_dividend_is_paid_end_to_end():
    from app.marketdata.synthetic import SyntheticFeed
    start = datetime(2027, 1, 1, 12, tzinfo=timezone.utc).timestamp()
    clock = SimClock(start)
    feed = SyntheticFeed(seed=1, now=start, history_days=30)
    [act] = [x for x in feed.corporate_actions(date(2027, 1, 1), date(2027, 3, 31)) if x.symbol == "ORBT"]
    ex = Exchange(feed, clock)
    ex.tick()
    a = acct(ex)
    ex.submit(a, {"symbol": "ORBT", "side": "buy", "qty": 10})
    day = date(2027, 1, 1)
    while day < act.pay_date:
        day += timedelta(days=1)
        clock.set(datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp())
        ex.tick()
    [t] = txns(ex, a, "dividend")
    assert t.postings == [("cash", D("7.50")), ("income:dividends", D("-7.50"))]
    ledger_ok(ex.account(a))


def test_state_file_from_before_lifecycle_loads(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    ex.save(force=True)
    path = tmp_path / "state.json"
    data = json.loads(path.read_text())
    del data["lifecycle"]
    for x in data["accounts"]:
        del x["settings"], x["lifecycle"]
    path.write_text(json.dumps(data))
    ex2 = Exchange(SimFeed(), SimClock(clock.now()), data_dir=str(tmp_path))
    assert ex2.load()
    ex2.feed.set_quote("ABC", "99.99", "100", "100")
    ex2.tick()
    assert ex2.account(a).settings == cfg.defaults() and ex2.lifecycle.last_date == ex.lifecycle.last_date
    assert ex2.summary(a)["settled_cash"] == ex2.account(a).cash
