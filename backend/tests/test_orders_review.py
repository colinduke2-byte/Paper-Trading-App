"""Regression tests for the findings of the independent review of the orders stream."""
import time
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

from app.api.server import create_app
from app.core import calendar as cal
from app.core.clock import SimClock
from app.exchange import Exchange, OrderRejected
from app.marketdata.feed import SimFeed
from app.marketdata.synthetic import SyntheticFeed
from app.matching.multileg import net_price
from app.options.symbology import parse
from app.risk import margin
from helpers import D, T0, make_exchange, move, q
from test_multileg import (BULL_CALL, BULL_PUT, C100, C105, C110, C95, EX_SYMS, FAR, NEAR, P100, P90, P95, acct,
                           at_expiry, held, leg, legacy_requirement, market_net, opt)


def ny(*args):
    return datetime(*args, tzinfo=cal.NY).timestamp()


def cash_account_is_sound(ex, a):
    account = ex.account(a)
    assert account.cash >= 0, f"negative cash {account.cash}"
    assert all(n > 0 for s, n in account.positions().items() if not parse(s)), "short stock in a cash account"


# ---- 1. cash accounts and assignment --------------------------------------------------------
def test_cash_account_cannot_open_the_spreads_that_assignment_would_break():
    ex, feed, clock = make_exchange()
    far_put = opt("P", 100, FAR)
    for cash, legs, msg in (("600", [leg(P100, "sell"), leg(P95, "buy")], "insufficient cash to secure short options"),
                            ("600", [leg(P95, "buy"), leg(P100, "sell")], "insufficient cash to secure short options"),
                            ("600", [leg(C100, "sell"), leg(C105, "buy")], "uncovered calls need a margin account"),
                            ("2000", [leg(P100, "sell"), leg(far_put, "buy")], "insufficient cash to secure short options")):
        a = acct(ex, "cash", cash)
        with pytest.raises(OrderRejected, match=msg):
            ex.submit(a, {"legs": legs})
        assert not ex.preview_multileg(a, {"legs": legs})["ok"]
        assert not ex.positions(a)


def test_cash_secured_put_spread_takes_assignment_without_going_negative():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "10000")
    ex.submit(a, {"legs": [leg(P100, "sell"), leg(P95, "buy")]})
    cash = ex.summary(a)["cash"]
    at_expiry(ex, feed, clock, "97")  # pinned: assigned 100 shares at 100
    assert held(ex, a) == {"ABC": 100} and ex.summary(a)["cash"] == cash - D("10000") >= 0
    cash_account_is_sound(ex, a)


def test_cash_account_long_call_it_cannot_pay_for_is_closed_at_expiry_not_exercised():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "15000")
    ex.submit(a, {"symbol": C100, "side": "buy", "qty": 3})
    ex.submit(a, {"symbol": "XYZ", "side": "buy", "qty": 1}) if ex.is_stock("XYZ") else None
    cash = ex.summary(a)["cash"]  # a little under 15,000: enough to exercise one call, not three
    at_expiry(ex, feed, clock, "108")
    account = ex.account(a)
    assert held(ex, a) == {"ABC": 100}  # one exercised: 100 shares at 100
    assert account.cash == cash - D("10000") + 2 * D("800")  # two closed for their 8.00 of intrinsic value
    kinds = [(f.kind, f.symbol, f.side, f.qty, f.price) for f in account.fills[-3:]]
    assert kinds == [("exercise", C100, "sell", 1, 0), ("exercise", "ABC", "buy", 100, D("100")),
                     ("closed at expiry", C100, "sell", 2, D("8"))]
    memo = account.ledger.transactions[-1]
    assert memo.kind == "closed at expiry" and memo.memo.startswith("Closed at expiry: sell 2 ABC")
    cash_account_is_sound(ex, a)


def test_cash_account_long_put_without_shares_is_closed_at_expiry_and_with_shares_is_exercised():
    ex, feed, clock = make_exchange()
    a, b = acct(ex, "cash", "20000"), acct(ex, "cash", "20000")
    ex.submit(a, {"symbol": P100, "side": "buy", "qty": 1})
    ex.submit(b, {"symbol": "ABC", "side": "buy", "qty": 100})
    ex.submit(b, {"symbol": P100, "side": "buy", "qty": 2})
    cash_a, cash_b = ex.summary(a)["cash"], ex.summary(b)["cash"]
    at_expiry(ex, feed, clock, "91")
    assert held(ex, a) == {} and ex.account(a).cash == cash_a + D("900")  # no shares to deliver: 9.00 in cash
    assert ex.account(a).fills[-1].kind == "closed at expiry"
    # b delivers its 100 shares on one put and is paid the intrinsic value on the other
    assert held(ex, b) == {} and ex.account(b).cash == cash_b + D("10000") + D("900")
    assert [f.kind for f in ex.account(b).fills[-3:]] == ["exercise", "exercise", "closed at expiry"]
    for x in (a, b):
        cash_account_is_sound(ex, x)


def test_margin_account_still_exercises_whatever_its_cash():
    ex, feed, clock = make_exchange()
    a = acct(ex, "margin", "3000")
    ex.submit(a, {"symbol": C100, "side": "buy", "qty": 3})
    at_expiry(ex, feed, clock, "108")
    assert held(ex, a) == {"ABC": 300} and ex.account(a).cash < 0
    assert all(f.kind == "exercise" for f in ex.account(a).fills[-2:])


def test_cash_account_buying_back_a_covered_call_may_not_spend_the_cash_behind_a_put():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "19600")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
    ex.submit(a, {"symbol": C100, "side": "sell", "qty": 1})
    ex.submit(a, {"symbol": P95, "side": "sell", "qty": 1})
    assert D("9500") < ex.summary(a)["cash"] < D("9900")
    move(ex, feed, clock, "ABC", "106")  # the call now costs more to buy back than the cash that is spare
    with pytest.raises(OrderRejected, match="insufficient cash to secure short options"):
        ex.submit(a, {"symbol": C100, "side": "buy", "qty": 1})
    ex.submit(a, {"symbol": P95, "side": "buy", "qty": 1, "type": "limit", "limit_price": "0.50", "tif": "gtc"})  # frees cash
    at_expiry(ex, feed, clock, "90")
    cash_account_is_sound(ex, a)


SINGLE_SYMS = EX_SYMS + ["ABC"]


@st.composite
def cash_script(draw):
    steps = []
    for _ in range(draw(st.integers(1, 10))):
        kind = draw(st.sampled_from(["single", "single", "single", "multi", "move"]))
        if kind == "move":
            steps.append(("move", Decimal(draw(st.integers(8000, 12000))) / 100))
        elif kind == "single":
            symbol = draw(st.sampled_from(SINGLE_SYMS))
            req = {"symbol": symbol, "side": draw(st.sampled_from(["buy", "sell"])),
                   "qty": draw(st.sampled_from([100, 100, 200, 50])) if symbol == "ABC" else draw(st.integers(1, 3))}
            if draw(st.booleans()):
                px = draw(st.integers(1, 1500)) if symbol != "ABC" else draw(st.integers(8000, 12000))
                req.update(type="limit", limit_price=str(Decimal(px) / 100), tif="gtc")
            steps.append(("order", req))
        else:
            symbols = draw(st.lists(st.sampled_from(SINGLE_SYMS), min_size=2, max_size=4, unique=True))
            legs = [leg(s, draw(st.sampled_from(["buy", "sell"])), 100 if s == "ABC" else draw(st.integers(1, 2))) for s in symbols]
            if any(s != "ABC" for s in symbols):
                steps.append(("order", {"legs": legs, "qty": draw(st.integers(1, 2))}))
    return steps


@settings(max_examples=250, deadline=None)
@given(cash_script(), st.integers(1000, 60000), st.integers(6000, 14000), st.integers(6000, 14000))
def test_cash_account_never_ends_an_expiry_with_negative_cash_or_short_stock(steps, cash, near_px, far_px):
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", str(cash))
    for step in steps:
        if step[0] == "move":
            move(ex, feed, clock, "ABC", step[1])
        else:
            try:
                ex.submit(a, step[1])
            except OrderRejected:
                pass
        cash_account_is_sound(ex, a)
    at_expiry(ex, feed, clock, Decimal(near_px) / 100, NEAR)
    cash_account_is_sound(ex, a)
    at_expiry(ex, feed, clock, Decimal(far_px) / 100, FAR)
    cash_account_is_sound(ex, a)
    assert not [s for s in ex.account(a).positions() if parse(s)]  # every option is gone
    led = ex.account(a).ledger
    assert sum(led.balances.values()) == 0 and all(sum(v for _, v in t.postings) == 0 for t in led.transactions)


# ---- 2. editing a held triggered order ---------------------------------------------------------
def test_raising_a_held_triggered_order_goes_through_the_risk_check():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "20000")
    entry, child = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "limit", "limit_price": "90", "tif": "gtc",
                                 "triggers": [{"symbol": "ABC", "side": "buy", "qty": 10, "type": "market"}]})
    with pytest.raises(OrderRejected, match="insufficient buying power for the change"):
        ex.replace(a, child.id, {"qty": 100000})
    assert child.qty == 10 and child.status == "held"
    ex.replace(a, child.id, {"qty": 50})  # affordable: accepted
    for px in ("89.99", "90.00", "90.01"):
        move(ex, feed, clock, "ABC", px)
    assert child.status == "filled" and held(ex, a) == {"ABC": 51}
    cash_account_is_sound(ex, a)
    # a held exit order is re-sized to what the entry filled, so editing it cannot add risk
    entry, exit_ = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "limit_price": "80", "tif": "gtc",
                                 "triggers": [{"type": "limit", "limit_price": "120"}]})
    ex.replace(a, exit_.id, {"qty": 5000})
    move(ex, feed, clock, "ABC", "79")
    assert exit_.status == "working" and exit_.qty == 10


# ---- 3. one-cancels-other cannot be made to over-fill -----------------------------------------------
def test_oco_quantity_cannot_be_raised_after_the_group_started_filling():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 20})
    A, B = ex.submit(a, {"oco": [{"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "limit_price": "99", "tif": "gtc"},
                                 {"symbol": "ABC", "side": "sell", "qty": 10, "type": "limit", "limit_price": "101", "tif": "gtc"}]})
    clock.advance(1)
    feed.set_quote("ABC", D("98.99"), D("99.00"), D("99.00"), ask_size=D(4))
    ex.tick()
    assert A.filled_qty == 4 and B.qty == 6
    with pytest.raises(OrderRejected, match="quantity of a one-cancels-other order cannot be raised"):
        ex.replace(a, B.id, {"qty": 10})
    assert B.qty == 6
    ex.replace(a, B.id, {"qty": 5, "limit_price": "100.50"})  # lowering it, or moving its price, is fine
    move(ex, feed, clock, "ABC", "101.01")
    assert B.status == "filled" and A.status == "canceled" and A.filled_qty + B.filled_qty == 9 <= 10
    # the same holds for bracket legs
    entry, tp, sl = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "take_profit": "120", "stop_loss": "90"})
    with pytest.raises(OrderRejected, match="cannot be raised"):
        ex.replace(a, tp.id, {"qty": 30})


# ---- 4. buying back a short leg is always risk-reducing -----------------------------------------------
def test_buying_back_the_short_leg_of_a_spread_is_allowed_in_a_margin_call():
    ex, feed, clock = make_exchange(ABC="100", XYZ="100")
    a = acct(ex)
    ex.submit(a, {"legs": BULL_CALL, "qty": 10})
    ex.submit(a, {"symbol": "XYZ", "side": "buy", "qty": int(ex.summary(a)["buying_power"] / D("100.5"))})
    move(ex, feed, clock, "XYZ", "70")
    s = ex.summary(a)
    assert s["equity"] < s["initial_requirement"]
    with pytest.raises(OrderRejected, match="insufficient buying power"):
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1})
    assert ex.preview(a, {"symbol": C105, "side": "buy", "qty": 10})["ok"]
    with pytest.raises(OrderRejected):  # more than the short position: that part would open a long
        ex.submit(a, {"symbol": C105, "side": "buy", "qty": 11, "type": "limit", "limit_price": "20"})
    assert ex.submit(a, {"symbol": C105, "side": "buy", "qty": 10})[0].status == "filled"
    assert held(ex, a)[C100] == 10 and C105 not in held(ex, a)
    short = acct(ex)
    ex.submit(short, {"symbol": "ABC", "side": "sell", "qty": 1000})
    move(ex, feed, clock, "ABC", "170")
    assert ex.summary(short)["margin_call"]
    assert ex.submit(short, {"symbol": "ABC", "side": "buy", "qty": 1000})[0].status == "filled"


# ---- 5. two closing orders cannot share one cover -------------------------------------------------------
def underwater_with_stock_and_call_spread(type_="margin"):
    ex, feed, clock = make_exchange(ABC="100", XYZ="100")
    a = acct(ex, type_)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
    assert ex.submit(a, {"legs": [leg(C100, "buy"), leg(C105, "sell")]})[0].status == "filled"
    if type_ == "margin":
        ex.submit(a, {"symbol": "XYZ", "side": "buy", "qty": int(ex.summary(a)["buying_power"] / D("100.5"))})
        move(ex, feed, clock, "XYZ", "70")
        assert ex.summary(a)["equity"] < ex.summary(a)["initial_requirement"]
    return ex, a


SELL_STOCK = {"symbol": "ABC", "side": "sell", "qty": 100, "type": "limit", "limit_price": "101", "tif": "gtc"}
SELL_CALL = {"symbol": C100, "side": "sell", "qty": 1, "type": "limit", "limit_price": "50", "tif": "gtc"}


def test_two_closing_orders_cannot_together_leave_a_naked_call_in_a_margin_call():
    ex, a = underwater_with_stock_and_call_spread()
    assert ex.submit(a, SELL_STOCK)[0].status == "working"  # the long call still covers the short call
    for req in (SELL_CALL, {"legs": [leg(C100, "sell"), leg(P95, "sell")]}):
        with pytest.raises(OrderRejected, match="insufficient buying power"):
            ex.submit(a, req)  # with the shares already for sale, this would uncover the short call
    ex.cancel(a, ex.orders(a, "open")[0].id)
    assert ex.submit(a, SELL_CALL)[0].status == "working"  # once the first is gone it is a plain closing order
    # Now the shares are what covers the short call. Giving them up lowers a margin account's
    # requirement (stock margin is larger than the uncovered-call requirement), so that is
    # still a risk-reducing trade there; a cash account refuses it (next test).
    assert ex.submit(a, SELL_STOCK)[0].status == "working"


def test_cash_account_cannot_sell_the_shares_behind_a_short_call_in_any_order():
    ex, a = underwater_with_stock_and_call_spread("cash")
    with pytest.raises(OrderRejected, match="uncovered calls need a margin account"):
        ex.submit(a, SELL_STOCK)  # the long call is not cover in a cash account
    assert ex.submit(a, SELL_CALL)[0].status == "working"
    with pytest.raises(OrderRejected, match="uncovered calls need a margin account"):
        ex.submit(a, SELL_STOCK)
    # a resting sale of a new call and a resting sale of the shares that would cover it
    ex2, feed, clock = make_exchange()
    b = acct(ex2, "cash")
    ex2.submit(b, {"symbol": "ABC", "side": "buy", "qty": 100})
    assert ex2.submit(b, dict(SELL_CALL, symbol=C105))[0].status == "working"
    with pytest.raises(OrderRejected, match="uncovered calls need a margin account"):
        ex2.submit(b, SELL_STOCK)


@st.composite
def closing_script(draw):
    opening = [draw(st.sampled_from([("ABC", 100), ("ABC", 200), ("ABC", 300)]))]
    opening += [(s, draw(st.integers(1, 3))) for s in draw(st.lists(st.sampled_from(EX_SYMS), max_size=3, unique=True))]
    orders = []
    for _ in range(draw(st.integers(1, 8))):
        symbol = draw(st.sampled_from(SINGLE_SYMS))
        side = draw(st.sampled_from(["sell", "sell", "buy"]))
        qty = draw(st.sampled_from([100, 200])) if symbol == "ABC" else draw(st.integers(1, 3))
        # resting orders: sells far above the market, buys far below it
        px = ("500" if symbol == "ABC" else "400") if side == "sell" else ("1" if symbol == "ABC" else "0.01")
        orders.append({"symbol": symbol, "side": side, "qty": qty, "type": "limit", "limit_price": px, "tif": "gtc"})
    return opening, orders


@settings(max_examples=250, deadline=None)
@given(closing_script())
def test_accepted_orders_all_filling_never_leave_a_cash_account_uncovered(script):
    """No sequence of accepted orders, all filling, yields a book the account could not open directly."""
    opening, orders = script
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "1000000")
    for symbol, qty in opening:
        ex.submit(a, {"symbol": symbol, "side": "buy", "qty": qty})
    for req in orders:
        try:
            ex.submit(a, req)
        except OrderRejected:
            continue
        book = dict(ex.account(a).positions())
        sells_only = dict(book)
        for o in ex.orders(a, "open"):
            delta = o.remaining if o.side == "buy" else -o.remaining
            book[o.symbol] = book.get(o.symbol, D(0)) + delta
            if o.side == "sell":
                sells_only[o.symbol] = sells_only.get(o.symbol, D(0)) + delta
        for final in (book, sells_only):
            final = {s: n for s, n in final.items() if n}
            ev = margin.evaluate("cash", D("1000000000"), final, ex._marks(final))
            assert ev.ok, (ev.reason, final)


# ---- 6. linked orders cannot nest ----------------------------------------------------------------
def test_linked_children_cannot_be_multileg_or_linked():
    ex, _, _ = make_exchange()
    a = acct(ex)
    single = {"symbol": "ABC", "side": "buy", "qty": 1}
    lim = dict(single, type="limit", limit_price="90", tif="gtc")
    for req in (dict(single, triggers=[{"legs": BULL_CALL}]),
                dict(single, triggers=[dict(lim, triggers=[lim])]),
                dict(single, triggers=[{"oco": [lim, lim]}]),
                {"oco": [lim, {"legs": BULL_CALL}]},
                {"oco": [lim, dict(lim, oco=[lim, lim])]},
                {"oco": [lim, dict(lim, triggers=[lim])]}):
        with pytest.raises(OrderRejected, match="a linked order cannot itself be multi-leg or linked"):
            ex.submit(a, req)
        with pytest.raises(OrderRejected, match="a linked order cannot itself be multi-leg or linked"):
            ex.preview(a, req)
    assert not ex.orders(a)


# ---- 7. pairing long and short options -------------------------------------------------------------
def test_requirement_uses_the_best_valid_pairing():
    near_c100, far_c110, far_c90, near_c105 = C100, opt("C", 110, FAR), opt("C", 90, FAR), C105
    pos = {near_c100: D(-1), far_c110: D(-1), far_c90: D(1), near_c105: D(1)}
    marks = {"ABC": D("100"), near_c100: D("2.00"), far_c110: D("1.50"), far_c90: D("11.50"), near_c105: D("0.50")}
    # near short with near long (5.00 wide), far short with far long (no strike risk):
    # net value (11.50 + 0.50 - 2.00 - 1.50) x 100 = 850, plus 500 of strike risk
    assert margin.requirement(pos, marks) == D("1350")
    # the first-come pairing gives the far long to the near short and leaves the far short uncovered
    _, groups, naked, free = margin._cover(D(0), pos)
    assert naked == {far_c110: D(1)} and free == {near_c105: D(1)}
    # every candidate is a valid cover: same kind, and the long expires on or after the short
    for groups, naked, free in margin._covers(D(0), pos)[1]:
        for expiry, g in groups.items():
            assert all(parse(s).expiry >= expiry for s, n in g.items() if n > 0)
            assert sum(g.values()) == 0


def all_pairings(shorts, longs):
    """Every way to pair short contracts with long contracts that may cover them (brute force)."""
    cells = [(s, l) for s in shorts for l in longs
             if parse(s).kind == parse(l).kind and parse(l).expiry >= parse(s).expiry]

    def walk(i, left_s, left_l, acc):
        if i == len(cells):
            yield dict(acc)
            return
        s, l = cells[i]
        for n in range(int(min(left_s[s], left_l[l])) + 1):
            left_s[s] -= n
            left_l[l] -= n
            if n:
                acc[(s, l)] = D(n)
            yield from walk(i + 1, left_s, left_l, acc)
            acc.pop((s, l), None)
            left_s[s] += n
            left_l[l] += n

    yield from walk(0, dict(shorts), dict(longs), {})


PAIR_UNIVERSE = [opt(k, s, e) for k in "CP" for s in (90, 100, 110) for e in (NEAR, FAR)]
px = st.integers(1, 3000).map(lambda c: Decimal(c) / 100)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(PAIR_UNIVERSE), min_size=2, max_size=5, unique=True), st.data())
def test_requirement_is_the_minimum_over_all_valid_pairings(symbols, data):
    pos = {s: D(data.draw(st.integers(-2, 2))) for s in symbols}
    pos = {s: n for s, n in pos.items() if n}
    if not pos:
        return
    marks = {s: data.draw(px) for s in pos}
    marks["ABC"] = Decimal(data.draw(st.integers(6000, 14000))) / 100
    shorts = {s: -n for s, n in pos.items() if n < 0}
    longs = {s: n for s, n in pos.items() if n > 0}
    best = None
    for pairs in all_pairings(shorts, longs):
        groups, naked, free = margin._split(shorts, pos, pairs.items())
        cost = (sum((n * marks[s] for s, n in free.items()), D(0)) * 100
                + sum((margin._group_requirement(g, marks) for g in groups.values()), D(0))
                + margin._uncovered_requirement(naked, marks))
        best = cost if best is None else min(best, cost)
    best = best.quantize(D("0.01"), "ROUND_HALF_UP")
    got = margin.requirement(pos, marks)
    assert best <= got <= legacy_requirement(pos, marks)  # never below the best valid pairing
    if not shorts or not longs or margin._pairing_count(shorts, longs) <= margin._MAX_EXACT:
        assert got == best  # and for all but very large books it is exactly the best


def test_large_books_fall_back_to_greedy_pairing_and_stay_sound():
    pos = {opt("C", k, e): D(n) for k, e, n in ((90, FAR, 7), (95, FAR, 5), (100, FAR, 3), (105, NEAR, 4),
                                                (100, NEAR, -6), (110, NEAR, -5), (110, FAR, -4), (115, FAR, -3))}
    shorts = {s: -n for s, n in pos.items() if n < 0}
    longs = {s: n for s, n in pos.items() if n > 0}
    assert margin._pairing_count(shorts, longs) > margin._MAX_EXACT
    marks = {s: model_mark(s, D(100)) for s in pos} | {"ABC": D(100)}
    got = margin.requirement(pos, marks)
    loss = sum((n * marks[s] for s, n in pos.items()), D(0)) * 100 - min(value_at(pos, D(p)) for p in range(0, 301, 5))
    assert min(loss, legacy_requirement(pos, marks)) <= got <= legacy_requirement(pos, marks)
    # a 10-lot butterfly is searched as a 1-lot one
    assert margin._pairing_count({C100: D(20)}, {C95: D(10), C105: D(10)}) == 4


def model_mark(symbol, spot):
    c = parse(symbol)
    intrinsic = max(spot - c.strike, D(0)) if c.kind == "C" else max(c.strike - spot, D(0))
    return intrinsic + (D("0.5") if c.expiry == NEAR else D("1.5"))


def value_at(pos, price):
    return sum((n * 100 * (max(price - parse(s).strike, D(0)) if parse(s).kind == "C" else max(parse(s).strike - price, D(0)))
                for s, n in pos.items()), D(0))


@settings(max_examples=500, deadline=None)
@given(st.lists(st.sampled_from(PAIR_UNIVERSE + EX_SYMS), min_size=1, max_size=5, unique=True),
       st.integers(8000, 12000), st.data())
def test_requirement_never_understates_the_worst_loss_of_a_covered_book(symbols, spot, data):
    """Brute force over every price: with every short covered, the requirement holds the worst loss."""
    spot = Decimal(spot) / 100
    pos = {s: D(data.draw(st.integers(-3, 3))) for s in set(symbols)}
    pos = {s: n for s, n in pos.items() if n}
    if not pos:
        return
    marks = {s: model_mark(s, spot) for s in pos}
    marks["ABC"] = spot
    _, candidates = margin._covers(D(0), pos)
    grid = [D(p) for p in range(0, 301, 5)]
    worst = min(value_at(pos, p) for p in grid)
    loss = sum((n * marks[s] for s, n in pos.items()), D(0)) * 100 - worst
    full = 0
    for groups, naked, free in candidates:
        if naked:
            continue  # an uncovered short is charged by the uncovered formula, which is not a loss bound
        full += 1
        cost = (sum((n * marks[s] for s, n in free.items()), D(0)) * 100
                + sum((margin._group_requirement(g, marks) for g in groups.values()), D(0)))
        assert cost >= loss - D("0.01"), (groups, free)
    got = margin.requirement(pos, marks)
    assert D(0) <= got <= legacy_requirement(pos, marks)


# ---- 8. on-open and on-close orders across a restart -----------------------------------------------
def us_exchange(tmp, start):
    clock, feed = SimClock(start), SimFeed()
    ex = Exchange(feed, clock, data_dir=tmp, session_mode="us")
    feed.set_quote("ABC", "99.99", "100", "100")
    ex.tick()
    return ex, feed, clock


def restart(ex, feed, clock, tmp):
    ex.save(force=True)
    ex2 = Exchange(feed, clock, data_dir=tmp, session_mode="us")
    assert ex2.load()
    return ex2


def test_auction_order_carries_its_auction_time():
    ex, feed, clock = us_exchange(None, ny(2026, 11, 24, 8, 0))  # Tuesday, before the open
    a = acct(ex)
    [moo] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 5, "type": "market_on_open"})
    [moc] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 5, "type": "market_on_close"})
    assert moo.auction_at == ny(2026, 11, 24, 9, 30) and moc.auction_at == ny(2026, 11, 24, 16, 0)
    clock.set(ny(2026, 11, 25, 17, 0))  # Wednesday evening; Thursday is Thanksgiving, Friday closes at 13:00
    [late_open] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 5, "type": "market_on_open"})
    [late_close] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 5, "type": "market_on_close"})
    assert late_open.auction_at == ny(2026, 11, 27, 9, 30) and late_close.auction_at == ny(2026, 11, 27, 13, 0)
    always, _, _ = make_exchange()
    [x] = always.submit(acct(always), {"symbol": "ABC", "side": "buy", "qty": 5, "type": "limit_on_close", "limit_price": "100"})
    assert x.auction_at == T0 - T0 % 86400 + 86400


def test_close_uses_the_last_regular_session_price():
    ex, feed, clock = us_exchange(None, ny(2026, 11, 24, 8, 0))
    a = acct(ex)
    [moc] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 5, "type": "market_on_close"})
    clock.set(ny(2026, 11, 24, 15, 59))
    feed.set_quote("ABC", "104.99", "105", "105")
    ex.tick()
    clock.set(ny(2026, 11, 24, 16, 0))
    feed.set_quote("ABC", "109.99", "110", "110")  # arrives with the first tick after the bell
    ex.tick()
    assert moc.status == "filled" and moc.avg_fill_price == D("105")


def test_on_close_order_missed_while_stopped_expires_instead_of_waiting_a_day(tmp_path):
    tmp = str(tmp_path)
    ex, feed, clock = us_exchange(tmp, ny(2026, 11, 24, 10, 0))
    a = acct(ex)
    [moc] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 5, "type": "market_on_close"})
    [moo] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 5, "type": "limit_on_open", "limit_price": "200"})
    clock.set(ny(2026, 11, 24, 15, 0))
    ex.tick()
    ex2 = restart(ex, feed, clock, tmp)  # stopped at 15:00, started again at 16:30
    assert ex2.engine.orders[moc.id].auction_at == ny(2026, 11, 24, 16, 0)
    clock.set(ny(2026, 11, 24, 16, 30))
    ex2.tick()
    missed = ex2.engine.orders[moc.id]
    assert missed.status == "expired" and missed.filled_qty == 0
    assert missed.reason == "the close passed while the simulator was not running"
    assert ex2.engine.orders[moo.id].status == "working"  # tomorrow's open is still ahead
    ex3 = restart(ex2, feed, clock, tmp)  # stopped overnight, started after the open
    clock.set(ny(2026, 11, 25, 11, 0))
    ex3.tick()
    assert ex3.engine.orders[moo.id].status == "expired" and "open passed" in ex3.engine.orders[moo.id].reason
    clock.set(ny(2026, 11, 25, 16, 1))
    ex3.tick()
    assert ex3.engine.orders[moc.id].status == "expired" and not ex3.positions(a)


def test_on_close_order_survives_a_restart_before_the_close(tmp_path):
    tmp = str(tmp_path)
    ex, feed, clock = us_exchange(tmp, ny(2026, 11, 24, 10, 0))
    a = acct(ex)
    [moc] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 5, "type": "market_on_close"})
    ex2 = restart(ex, feed, clock, tmp)
    clock.set(ny(2026, 11, 24, 15, 30))
    feed.set_quote("ABC", "102.99", "103", "103")
    ex2.tick()
    clock.set(ny(2026, 11, 24, 16, 0, 5))
    ex2.tick()
    o = ex2.engine.orders[moc.id]
    assert o.status == "filled" and o.avg_fill_price == D("103")


# ---- 9. absurd numbers --------------------------------------------------------------------------------
def test_huge_numbers_are_rejected_not_crashed_on():
    ex, _, _ = make_exchange()
    a = acct(ex)
    single = {"symbol": "ABC", "side": "buy", "qty": 1}
    huge = [dict(single, type="limit", limit_price="1e30"), dict(single, qty="1e30"),
            dict(single, type="stop", stop_price="1e30"), dict(single, type="trailing_stop", trail_amount="1e30"),
            dict(single, take_profit="1e30"), dict(single, stop_loss="1E+40"),
            {"legs": BULL_CALL, "type": "limit", "limit_price": "1e30", "price_effect": "debit"},
            {"legs": BULL_CALL, "qty": "1e30"}, {"legs": [leg(C100, "buy", "1e30"), leg(C105, "sell")]},
            {"oco": [dict(single, type="limit", limit_price="1e30", tif="gtc")] * 2},
            dict(single, triggers=[dict(single, type="limit", limit_price="1e30")])]
    for req in huge:
        for call in (ex.submit, ex.preview):
            with pytest.raises(OrderRejected, match="too large|quantity must be"):
                call(a, req)
    with pytest.raises(OrderRejected, match="too large"):
        ex.preview_multileg(a, huge[6])
    [o] = ex.submit(a, dict(single, type="limit", limit_price="90", tif="gtc"))
    for change in ({"limit_price": "1e30"}, {"qty": "1e30"}):
        with pytest.raises(OrderRejected, match="too large"):
            ex.replace(a, o.id, change)
    assert o.limit_price == D("90") and o.qty == 1
    # tiny exponents are just too many decimal places
    with pytest.raises(OrderRejected, match="decimal"):
        ex.submit(a, dict(single, type="limit", limit_price="1e-30"))


def test_huge_numbers_get_a_4xx_on_every_route():
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()))
    aid = ex.create_account("Margin", "margin").id
    ex.tick()
    c = TestClient(create_app(ex, run_loop=False), raise_server_exceptions=False)
    legs = [leg("NVLX", "buy", 100), leg("NVLX", "sell", 100)]
    single = {"symbol": "NVLX", "side": "buy", "qty": 1, "type": "limit", "limit_price": 1e30}
    multi = {"legs": legs, "type": "limit", "limit_price": 1e30, "price_effect": "debit"}
    for path, body in ((f"/api/accounts/{aid}/orders", single), (f"/api/accounts/{aid}/orders/preview", single),
                       (f"/api/accounts/{aid}/orders", multi), (f"/api/accounts/{aid}/orders/preview", multi),
                       (f"/api/accounts/{aid}/multileg", multi), (f"/api/accounts/{aid}/multileg/preview", multi)):
        r = c.post(path, json=body)
        assert r.status_code == 422 and r.json()["rejected"], (path, r.status_code)
    oid = c.post(f"/api/accounts/{aid}/orders", json=dict(single, limit_price=1)).json()[0]["id"]
    assert c.patch(f"/api/accounts/{aid}/orders/{oid}", json={"limit_price": 1e30}).status_code == 422


# ---- 10. zero bids and date format ------------------------------------------------------------------------
def test_multileg_never_sells_a_leg_for_nothing():
    quotes = {C100: q(2.90, 3.10, symbol=C100), C105: q(0, 0.02, symbol=C105)}
    assert net_price(BULL_CALL, quotes) is None  # would sell C105 at a zero bid
    assert net_price([leg(C100, "sell"), leg(C105, "buy")], quotes)[0] == D("-2.88")  # buying it is fine
    ex, feed, clock = make_exchange()
    a = acct(ex)
    worthless = opt("C", 130)
    assert ex.quote(worthless).bid == 0
    legs = [leg(C100, "buy"), leg(worthless, "sell")]
    for call in (ex.submit, ex.preview):
        with pytest.raises(OrderRejected, match="a leg to be sold needs a bid above zero"):
            call(a, {"legs": legs})
    # as a limit order it is accepted and waits until the leg can actually be sold
    [o] = ex.submit(a, {"legs": legs, "type": "limit", "limit_price": "9", "price_effect": "debit", "tif": "gtc"})
    assert o.status == "working" and not ex.account(a).fills
    move(ex, feed, clock, "ABC", "128")
    assert ex.quote(worthless).bid > 0 and o.status == "working"  # now priced, but far above the limit
    assert not ex.account(a).fills


def test_good_till_date_must_be_written_with_dashes():
    ex, _, _ = make_exchange()
    a = acct(ex)
    base = {"symbol": "ABC", "side": "buy", "qty": 1, "type": "limit", "limit_price": "90", "tif": "gtd"}
    for bad in ("20261231", "2026-W52-4", "2026-12-31T00:00", 20261231, "31/12/2026"):
        with pytest.raises(OrderRejected, match="must look like 2026-12-31"):
            ex.submit(a, dict(base, good_till_date=bad))
    assert ex.submit(a, dict(base, good_till_date="2026-12-31"))[0].good_till == "2026-12-31"
