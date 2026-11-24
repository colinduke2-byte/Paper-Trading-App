"""Multi-leg option orders: net pricing, all-or-none fills, strategy margin, preview and expiry."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

from app.api.server import create_app
from app.core import calendar as cal
from app.core.clock import SimClock
from app.core.models import Order
from app.exchange import Exchange, OrderRejected
from app.ledger.ledger import position_account
from app.marketdata.feed import SimFeed
from app.marketdata.synthetic import SyntheticFeed
from app.matching.engine import MatchingEngine
from app.matching.multileg import net_price, payoff
from app.options import chain
from app.options.symbology import build, multiplier, parse
from app.risk import margin
from helpers import D, T0, fills, make_exchange, move, q

EXPIRIES = chain.expirations(cal.ny_date(T0))
NEAR, FAR = EXPIRIES[0], EXPIRIES[2]


def opt(kind, strike, expiry=NEAR, underlying="ABC"):
    return build(underlying, expiry, kind, D(str(strike)))


def leg(symbol, side, ratio=1):
    return {"symbol": symbol, "side": side, "ratio": ratio}


def acct(ex, type="margin", cash="100000"):
    return ex.create_account("t", type, D(cash)).id


def signed_net(legs, prices):
    """Net price per unit from {symbol: price}: what the fills actually cost."""
    return sum(((1 if l["side"] == "buy" else -1) * l["ratio"] * multiplier(l["symbol"]) * prices[l["symbol"]]
                for l in legs), D(0)) / 100


def market_net(ex, legs):
    return net_price(legs, {l["symbol"]: ex.quote(l["symbol"]) for l in legs})[0]


C95, C100, C105, C110 = opt("C", 95), opt("C", 100), opt("C", 105), opt("C", 110)
P90, P95, P100 = opt("P", 90), opt("P", 95), opt("P", 100)
BULL_CALL = [leg(C100, "buy"), leg(C105, "sell")]
BULL_PUT = [leg(P95, "sell"), leg(P90, "buy")]
CONDOR = [leg(P90, "buy"), leg(P95, "sell"), leg(C105, "sell"), leg(C110, "buy")]
BUTTERFLY = [leg(C95, "buy"), leg(C100, "sell", 2), leg(C105, "buy")]


# ---- pure helpers ------------------------------------------------------------------
def test_net_price_buys_at_the_ask_and_sells_at_the_bid():
    quotes = {C100: q(2.90, 3.10, symbol=C100), C105: q(1.10, 1.30, symbol=C105)}
    net, prices = net_price(BULL_CALL, quotes)
    assert net == D("2.00") and prices == [D("3.10"), D("1.10")]
    net, _ = net_price([leg(C100, "sell"), leg(C105, "buy")], quotes)
    assert net == D("-1.60")  # a credit, and the two directions do not net to zero: the spread is paid twice
    assert net_price(BULL_CALL, {C100: quotes[C100]}) is None
    assert net_price(BULL_CALL, {C100: quotes[C100], C105: q(0, 0, symbol=C105)}) is None
    # stock leg: 100 shares per unit count at full price
    net, _ = net_price([leg("ABC", "buy", 100), leg(C105, "sell")], {"ABC": q(99.99, 100.00), C105: quotes[C105]})
    assert net == D("98.90")


def test_payoff_of_common_strategies():
    v = payoff(BULL_CALL, D("2.00"))
    assert (v["max_loss"], v["max_profit"], v["break_evens"]) == (D("200.00"), D("300.00"), [D("102.00")])
    assert not v["max_loss_unlimited"] and not v["max_profit_unlimited"] and v["at_expiry"]
    v = payoff(BULL_PUT, D("-0.90"))
    assert (v["max_loss"], v["max_profit"], v["break_evens"]) == (D("410.00"), D("90.00"), [D("94.10")])
    v = payoff(CONDOR, D("-1.70"))
    assert (v["max_loss"], v["max_profit"], v["break_evens"]) == (D("330.00"), D("170.00"), [D("93.30"), D("106.70")])
    v = payoff(BUTTERFLY, D("1.20"))
    assert (v["max_loss"], v["max_profit"], v["break_evens"]) == (D("120.00"), D("380.00"), [D("96.20"), D("103.80")])
    v = payoff([leg(C100, "buy"), leg(P100, "buy")], D("6.00"))  # long straddle
    assert v["max_loss"] == D("600.00") and v["max_profit"] is None and v["max_profit_unlimited"]
    assert v["break_evens"] == [D("94.00"), D("106.00")]
    v = payoff([leg(C105, "sell"), leg(P95, "sell")], D("-2.70"))  # short strangle
    assert v["max_loss"] is None and v["max_loss_unlimited"] and v["max_profit"] == D("270.00")
    assert v["break_evens"] == [D("92.30"), D("107.70")]
    v = payoff([leg("ABC", "buy", 100), leg(C105, "sell")], D("98.90"))  # covered call
    assert (v["max_loss"], v["max_profit"], v["break_evens"]) == (D("9890.00"), D("610.00"), [D("98.90")])
    v = payoff([leg("ABC", "buy", 100), leg(P95, "buy")], D("101.50"))  # protective put
    assert v["max_loss"] == D("650.00") and v["max_profit_unlimited"] and v["break_evens"] == [D("101.50")]
    v = payoff([leg(C100, "sell"), leg(opt("C", 100, FAR), "buy")], D("1.50"))  # calendar
    assert not v["at_expiry"] and v["max_loss"] == D("150.00") and v["max_profit"] is None and v["break_evens"] == []
    v = payoff([leg(C100, "buy"), leg(opt("C", 100, FAR), "sell")], D("-1.50"))  # reverse calendar: not bounded
    assert v["max_loss"] is None


# ---- engine ----------------------------------------------------------------------------
def combo(legs, qty=1, limit=None, tif="day"):
    combo.n = getattr(combo, "n", 0) + 1
    return Order(id=f"M{combo.n}", account_id="A1", symbol="ABC", side="buy", qty=D(qty), type="multileg",
                 limit_price=None if limit is None else D(str(limit)), tif=tif, legs=legs)


def test_engine_fills_every_leg_at_once_or_none():
    e = MatchingEngine()
    o = combo(BULL_CALL, qty=3, limit="1.90")
    quotes = {C100: q(2.90, 3.10, symbol=C100), C105: q(1.10, 1.30, symbol=C105)}
    ev = e.submit_multileg(o, quotes, 1)
    assert not fills(ev) and o.status == "working" and o.filled_qty == 0  # net 2.00 is above the 1.90 limit
    # one leg alone becoming attractive is not enough
    assert not fills(e.on_multileg(o.id, {C100: q(2.90, 3.05, symbol=C100), C105: quotes[C105]}, 2))
    # a quote for the underlying never touches a multi-leg order
    assert not e.on_quote(q(99.99, 100.00), 3) and o.status == "working"
    assert not fills(e.on_multileg(o.id, {C100: q(2.80, 3.00, symbol=C100)}, 4))  # a leg without a quote
    assert not fills(e.on_multileg(o.id, {C100: q(2.80, 3.00, symbol=C100), C105: quotes[C105]}, 5, "closed"))
    f = fills(e.on_multileg(o.id, {C100: q(2.80, 3.00, symbol=C100), C105: quotes[C105]}, 6))
    assert [(x.symbol, x.side, x.qty, x.price, x.order_id) for x in f] == [
        (C100, "buy", 3, D("3.00"), o.id), (C105, "sell", 3, D("1.10"), o.id)]
    assert o.status == "filled" and o.filled_qty == 3 and o.avg_fill_price == D("1.9000")
    assert not e.on_multileg(o.id, quotes, 7)  # finished orders never trade again


def test_engine_market_credit_ratio_and_immediate_orders():
    e = MatchingEngine()
    quotes = {C95: q(5.90, 6.10, symbol=C95), C100: q(2.90, 3.10, symbol=C100), C105: q(1.10, 1.30, symbol=C105)}
    fly = combo(BUTTERFLY, qty=2)
    f = fills(e.submit_multileg(fly, quotes, 1))
    assert [(x.symbol, x.side, x.qty, x.price) for x in f] == [
        (C95, "buy", 2, D("6.10")), (C100, "sell", 4, D("2.90")), (C105, "buy", 2, D("1.30"))]
    assert fly.avg_fill_price == D("1.6000")
    credit = combo([leg(C100, "sell"), leg(C105, "buy")], limit="-1.70")  # wants at least 1.70 credit; market pays 1.60
    assert not fills(e.submit_multileg(credit, quotes, 2)) and credit.status == "working"
    f = fills(e.on_multileg(credit.id, {C100: q(3.00, 3.20, symbol=C100), C105: quotes[C105]}, 3))
    assert credit.avg_fill_price == D("-1.7000") and len(f) == 2
    fok = combo(BULL_CALL, limit="1.00", tif="fok")
    e.submit_multileg(fok, quotes, 4)
    assert fok.status == "canceled" and fok.filled_qty == 0
    ioc = combo(BULL_CALL, limit="2.00", tif="ioc")
    e.submit_multileg(ioc, quotes, 5)
    assert ioc.status == "filled"
    waiting = combo(BULL_CALL)
    assert not fills(e.submit_multileg(waiting, quotes, 6, "closed")) and waiting.status == "working"
    e.cancel(waiting.id, 7)
    assert not e.on_multileg(waiting.id, quotes, 8)


cents_px = st.integers(1, 3000).map(lambda c: Decimal(c) / 100)
SYMS = [C95, C100, C105, C110, P90, P95, P100, "ABC"]


@st.composite
def random_legs(draw, stock=True):
    symbols = draw(st.lists(st.sampled_from(SYMS if stock else SYMS[:-1]), min_size=2, max_size=4, unique=True))
    if symbols == ["ABC"]:
        symbols.append(C100)
    return [leg(s, draw(st.sampled_from(["buy", "sell"])), 100 * draw(st.integers(1, 2)) if s == "ABC" else draw(st.integers(1, 3)))
            for s in symbols]


@st.composite
def leg_quotes(draw, legs):
    out = {}
    for l in legs:
        if draw(st.integers(0, 12)) == 0:
            continue  # sometimes a leg has no quote
        bid = draw(cents_px) - Decimal("0.01")
        out[l["symbol"]] = q(bid, bid + Decimal(draw(st.integers(1, 40))) / 100, symbol=l["symbol"])
    return out


@st.composite
def multileg_case(draw):
    legs = draw(random_legs())
    limit = draw(st.one_of(st.none(), st.integers(-6000, 6000).map(lambda c: Decimal(c) / 100)))
    tape = [(draw(leg_quotes(legs)), draw(st.sampled_from(["regular", "regular", "regular", "closed", "pre"])))
            for _ in range(draw(st.integers(1, 12)))]
    return legs, limit, draw(st.integers(1, 20)), draw(st.sampled_from(["day", "gtc", "ioc", "fok"])), tape


@settings(max_examples=500, deadline=None)
@given(multileg_case())
def test_multileg_is_all_or_none_and_never_worse_than_its_limit(case):
    legs, limit, qty, tif, tape = case
    e = MatchingEngine()
    o = combo(legs, qty=qty, limit=limit, tif=tif)
    done_at = None
    for i, (quotes, session) in enumerate(tape):
        ev = e.submit_multileg(o, quotes, i, session) if i == 0 else e.on_multileg(o.id, quotes, i, session)
        new = fills(ev)
        if not new:
            continue
        assert done_at is None, "a multi-leg order fills exactly once"
        done_at = i
        assert session == "regular"
        # every leg, in full, in the same instant, at its own side of its own quote
        assert [(f.symbol, f.side, f.qty) for f in new] == [(l["symbol"], l["side"], qty * l["ratio"]) for l in legs]
        for f, l in zip(new, legs):
            assert f.price == (quotes[f.symbol].ask if l["side"] == "buy" else quotes[f.symbol].bid)
            assert f.order_id == o.id and f.ts == i
        net = signed_net(legs, {f.symbol: f.price for f in new})
        assert limit is None or net <= limit
        assert abs(o.avg_fill_price - net) <= Decimal("0.00005")
    assert o.filled_qty == (qty if done_at is not None else 0)
    assert (o.status == "filled") == (done_at is not None)
    if tif in ("ioc", "fok"):
        assert done_at in (None, 0) and o.status in ("filled", "canceled")
    else:
        assert o.status in ("filled", "working")


# ---- strategy margin: worked examples ----------------------------------------------------
MARKS = {"ABC": D("100"), C95: D("6.00"), C100: D("3.00"), C105: D("1.20"), C110: D("0.40"),
         P90: D("0.60"), P95: D("1.50"), P100: D("3.20"),
         opt("C", 100, FAR): D("4.50"), opt("C", 105, FAR): D("2.60"), opt("P", 95, FAR): D("2.80")}


def req(pos):
    return margin.requirement({s: D(n) for s, n in pos.items()}, MARKS)


def test_defined_risk_spreads_need_their_maximum_loss():
    assert req({P95: -1, P90: 1}) == D("410")  # 5.00 wide, 0.90 credit
    assert req({C100: 1, C105: -1}) == D("180")  # debit spread: just what it costs
    assert req({C100: -1, C105: 1}) == D("320")  # bear call: 5.00 wide, 1.80 credit
    assert req({P90: 1, P95: -1, C105: -1, C110: 1}) == D("330")  # iron condor: one wing can lose, 1.70 credit
    assert req({C95: 1, C100: -2, C105: 1}) == D("120")  # butterfly: its 1.20 debit
    assert req({P95: -3, P90: 3}) == D("1230")
    # the same numbers the payoff calculator gives
    for legs, net in ((BULL_PUT, "-0.90"), (BULL_CALL, "1.80"), (CONDOR, "-1.70"), (BUTTERFLY, "1.20")):
        pos = {l["symbol"]: l["ratio"] if l["side"] == "buy" else -l["ratio"] for l in legs}
        assert req(pos) == payoff(legs, D(net))["max_loss"]


def test_calendars_and_diagonals_need_the_long_to_outlive_the_short():
    far_c100, far_c105 = opt("C", 100, FAR), opt("C", 105, FAR)
    assert req({C100: -1, far_c100: 1}) == D("150")  # calendar: the debit
    assert req({C100: -1, far_c105: 1}) == D("460")  # diagonal: -0.40 credit... net value -40 plus 5.00 of strike risk
    # short the later option: the long is gone first, so the short is uncovered (4.50 + 20) and the long paid in full
    assert req({far_c100: -1, C100: 1}) == D("2450") + D("300")


def test_uncovered_remainder_and_straddles():
    assert req({C105: -2, C100: 1}) == D("180") + (D("1.20") + D("15")) * 100  # one covered, one naked
    # short strangle: the greater side (C105: 1.20 + 15 = 16.20) plus the other premium (1.50)
    assert req({C105: -1, P95: -1}) == D("1620") + D("150")
    assert req({C105: -1}) + req({P95: -1}) == D("1620") + D("1650")
    # different expiries can both lose, so nothing is netted
    far_put = opt("P", 95, FAR)
    assert req({C105: -1, far_put: -1}) == req({C105: -1}) + req({far_put: -1})
    # stock covers calls first; a long call covers what is left
    assert req({"ABC": 100, C105: -2, C100: 1}) == D("5000") + D("180")
    # pairing is never used when it would cost more than the uncovered formula
    marks = dict(MARKS, **{opt("P", 50): D("0.01"), opt("P", 40): D("0.01")})
    wide = margin.requirement({opt("P", 50): D(-1), opt("P", 40): D(1)}, marks)
    assert wide == D("1") + D("501") < D("1000")


def test_cash_account_spread_rules():
    """In a cash account a long option is not cover: assignment settles in shares."""
    ok = lambda cash, pos: margin.evaluate("cash", D(cash), {s: D(n) for s, n in pos.items()}, MARKS)
    # short calls need the shares, whatever long calls are held
    for pos in ({C105: -1}, {C100: 1, C105: -1}, {C100: -1, C105: 1}, {C95: 1, C100: -2, C105: 1},
                {C100: -1, opt("C", 100, FAR): 1}, {opt("C", 100, FAR): -1, C100: 1}, {C105: -1, P95: -1},
                {P90: 1, P95: -1, C105: -1, C110: 1}, {"ABC": 100, C105: -2, C100: 1}):
        ev = ok("1000000", pos)
        assert not ev.ok and ev.structural and "uncovered calls" in ev.reason
    # short puts need strike x 100 in cash, whatever long puts are held
    ev = ok("9499.99", {P95: -1, P90: 1})
    assert not ev.ok and ev.requirement == D("9500") and "secure short options" in ev.reason and not ev.structural
    assert ok("9500", {P95: -1, P90: 1}).ok and ok("9500", {P95: -1, P100: 1}).ok
    assert ok("9500", {P95: -1}).ok and not ok("9499", {P95: -1}).ok
    assert not ok("9499", {P95: -1, opt("P", 95, FAR): 1}).ok  # a calendar is no different
    assert ok("19000", {P95: -2, P90: 1}).ok and not ok("18999", {P95: -2, P90: 2}).ok
    # long options alone, covered calls and cash-secured puts are unchanged
    assert ok("0", {C100: 1, P95: 2}).ok and ok("0", {"ABC": 100, C105: -1}).ok
    assert ok("9500", {"ABC": 100, C105: -1, P95: -1}).ok and ok("0", {"ABC": 100, C105: -1, C110: 1}).ok


# ---- strategy margin: properties ------------------------------------------------------------
def legacy_requirement(positions, marks):
    """The rule before spreads existed: stock covers calls, every other short option is uncovered."""
    H = Decimal(100)
    total = Decimal(0)
    shares = {s: n for s, n in positions.items() if n > 0 and not parse(s)}
    for s, n in positions.items():
        if not parse(s):
            total += abs(n) * marks[s] / 2
        elif n > 0:
            total += n * marks[s] * H
    for s in sorted((s for s, n in positions.items() if n < 0 and parse(s)), key=lambda s: parse(s).strike):
        c, n, u = parse(s), -positions[s], marks[parse(s).underlying]
        if c.kind == "C":
            covered = min(n, shares.get(c.underlying, Decimal(0)) // H)
            shares[c.underlying] = shares.get(c.underlying, Decimal(0)) - covered * H
            n -= covered
            per = marks[s] + max(u / 5 - max(c.strike - u, Decimal(0)), u / 10)
        else:
            per = marks[s] + max(u / 5 - max(u - c.strike, Decimal(0)), c.strike / 10)
        total += n * per * H
    return total.quantize(Decimal("0.01"), "ROUND_HALF_UP")


OPTION_UNIVERSE = [opt(k, s, e) for k in "CP" for s in (85, 90, 95, 100, 105, 110, 115) for e in (NEAR, FAR)]


@st.composite
def option_book(draw):
    symbols = draw(st.lists(st.sampled_from(OPTION_UNIVERSE), min_size=1, max_size=6, unique=True))
    pos = {s: Decimal(draw(st.integers(-5, 5))) for s in symbols}
    pos["ABC"] = Decimal(draw(st.sampled_from([0, 0, 100, 250, 500, -100])))
    pos = {s: n for s, n in pos.items() if n != 0}
    marks = {s: draw(cents_px) for s in symbols}
    marks["ABC"] = Decimal(draw(st.integers(6000, 14000))) / 100
    return pos, marks


@settings(max_examples=600, deadline=None)
@given(option_book())
def test_requirement_is_never_negative_and_never_above_the_uncovered_rule(book):
    pos, marks = book
    r = margin.requirement(pos, marks)
    assert Decimal(0) <= r <= legacy_requirement(pos, marks)
    assert margin.requirement(pos, marks, maintenance=True) <= r
    if not any(n > 0 and parse(s) for s, n in pos.items()):
        shorts = {s: n for s, n in pos.items() if parse(s)}
        calls, puts = ([parse(s).expiry for s in shorts if parse(s).kind == k] for k in "CP")
        if not set(calls) & set(puts):  # no long options and no straddle: exactly the old rule
            assert r == legacy_requirement(pos, marks)
    # the spread requirement never exceeds the naked requirement of its short legs
    shorts_only = {s: n for s, n in pos.items() if n < 0 and parse(s)}
    longs_cost = sum((n * marks[s] * 100 for s, n in pos.items() if n > 0 and parse(s)), Decimal(0))
    stock_only = {s: n for s, n in pos.items() if not parse(s)}
    if "ABC" not in pos:
        assert r <= sum((margin.requirement({s: n}, marks) for s, n in shorts_only.items()), Decimal(0)) + longs_cost
    assert r >= margin.requirement(stock_only, marks) if stock_only else True
    # evaluating a cash account never crashes and agrees on structure
    ev = margin.evaluate("cash", Decimal("1000000000"), pos, marks)
    assert ev.ok or ev.structural


def intrinsic_value(pos, price):
    total = Decimal(0)
    for s, n in pos.items():
        c = parse(s)
        total += n * 100 * (max(price - c.strike, Decimal(0)) if c.kind == "C" else max(c.strike - price, Decimal(0)))
    return total


@settings(max_examples=400, deadline=None)
@given(option_book(), st.integers(0, 30000).map(lambda c: Decimal(c) / 100))
def test_covered_groups_hold_enough_for_any_price_at_expiry(book, price):
    """With every short covered, equity at the requirement means cash covers the worst expiry value."""
    pos, marks = book
    pos = {s: n for s, n in pos.items() if parse(s)}
    if not pos:
        return
    after_stock, groups, naked, free = margin._cover(Decimal(0), pos)
    for expiry, group in groups.items():
        assert sum((n for s, n in group.items() if parse(s).kind == "C"), Decimal(0)) == 0  # calls are balanced
        assert all(parse(s).expiry >= expiry for s, n in group.items() if n > 0)  # every long outlives its short
        assert all(parse(s).expiry == expiry for s, n in group.items() if n < 0)
        assert margin.worst_value(group) <= intrinsic_value(group, price)
        assert margin.worst_value(group) == min(intrinsic_value(group, p) for p in
                                                [Decimal(0)] + [parse(s).strike for s in group])
    # nothing is lost or invented by the cover: groups + naked + free add back up to the book
    rebuilt = {}
    for part in list(groups.values()) + [{s: -n for s, n in naked.items()}, free]:
        for s, n in part.items():
            rebuilt[s] = rebuilt.get(s, Decimal(0)) + n
    assert {s: n for s, n in rebuilt.items() if n} == pos


@settings(max_examples=300, deadline=None)
@given(random_legs(stock=False), st.integers(1, 5), st.data())
def test_fully_covered_structures_need_at_most_their_maximum_loss(legs, qty, data):
    """A structure whose shorts can all be covered never needs more than its maximum loss.

    (It can need less: each short is charged the cheaper of its spread and the uncovered
    formula. test_requirement_is_the_minimum_over_all_valid_pairings pins the exact value.)
    """
    marks = {l["symbol"]: data.draw(cents_px) for l in legs}
    marks["ABC"] = Decimal(data.draw(st.integers(6000, 14000))) / 100
    pos = {l["symbol"]: Decimal(qty * l["ratio"] * (1 if l["side"] == "buy" else -1)) for l in legs}
    _, groups, naked, free = margin._cover(Decimal(0), pos)
    if naked or free or len(groups) != 1 or len({parse(s).expiry for s in pos}) != 1:
        return  # (a long option that covers nothing is simply paid in full, on top of the spread)
    shape = payoff(legs, signed_net(legs, marks))
    assert not shape["max_loss_unlimited"]
    got = margin.requirement(pos, marks)
    assert got <= legacy_requirement(pos, marks)
    [group] = groups.values()
    if sum((n * marks[s] for s, n in group.items()), Decimal(0)) * 100 >= margin.worst_value(group):
        # (marks that price a spread below its own floor are impossible; there the rule only rounds up)
        assert got <= shape["max_loss"] * qty + Decimal("0.01") * qty


# ---- exchange: order entry --------------------------------------------------------------------
def test_spread_is_checked_as_one_unit_in_a_margin_account():
    ex, feed, clock = make_exchange()
    a = acct(ex, "margin", "1500")
    with pytest.raises(OrderRejected, match="insufficient buying power"):
        ex.submit(a, {"symbol": C100, "side": "sell", "qty": 1})  # alone it is an uncovered call
    credit = -market_net(ex, [leg(C100, "sell"), leg(C105, "buy")])
    [o] = ex.submit(a, {"legs": [leg(C100, "sell"), leg(C105, "buy")], "qty": 2})
    assert o.status == "filled" and o.type == "multileg" and o.symbol == "ABC" and o.fees == D("2.60")
    assert o.avg_fill_price == -credit and credit > 0
    assert ex.summary(a)["cash"] == D("1500") + credit * 200 - D("2.60")
    assert {p["symbol"]: p["qty"] for p in ex.positions(a)} == {C100: -2, C105: 2}
    two = [f for f in ex.account(a).fills if f.order_id == o.id]
    assert [(f.symbol, f.side, f.qty) for f in two] == [(C100, "sell", 2), (C105, "buy", 2)]
    assert two[0].price == ex.quote(C100).bid and two[1].price == ex.quote(C105).ask
    # the requirement is the two spreads' maximum loss, not two uncovered calls
    marks = {s: ex.mark(s) for s in (C100, C105)}
    assert ex.summary(a)["initial_requirement"] == D("1000") + (marks[C105] - marks[C100]) * 200 <= D("1000")
    with pytest.raises(OrderRejected, match="insufficient buying power"):
        ex.submit(a, {"legs": [leg(C100, "sell"), leg(C105, "buy")], "qty": 3})
    # selling only the long leg would leave the short calls uncovered
    with pytest.raises(OrderRejected, match="insufficient buying power"):
        ex.submit(a, {"symbol": C105, "side": "sell", "qty": 2})
    with pytest.raises(OrderRejected, match="insufficient buying power"):
        ex.submit(a, {"legs": [leg(C105, "sell"), leg(P95, "buy")], "qty": 2})
    # closing the whole spread as one order is always allowed
    [c] = ex.submit(a, {"legs": [leg(C100, "buy"), leg(C105, "sell")], "qty": 2})
    assert c.status == "filled" and not ex.positions(a)


def test_cash_account_spreads_need_the_short_leg_secured():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "5000")
    for legs in ([leg(C100, "sell"), leg(C105, "buy")], BULL_CALL, BUTTERFLY, CONDOR,
                 [leg(C100, "sell"), leg(opt("C", 100, FAR), "buy")]):
        with pytest.raises(OrderRejected, match="uncovered calls need a margin account"):
            ex.submit(a, {"legs": legs})
    with pytest.raises(OrderRejected, match="insufficient cash to secure short options"):
        ex.submit(a, {"legs": BULL_PUT})  # the short 95 put needs 9,500 whatever the long put is
    assert not ex.orders(a) and not ex.positions(a)
    rich = acct(ex, "cash", "9600")
    [o] = ex.submit(rich, {"legs": BULL_PUT})
    assert o.status == "filled" and ex.summary(rich)["buying_power"] == ex.summary(rich)["cash"] - D("9500")
    with pytest.raises(OrderRejected, match="insufficient cash"):
        ex.submit(rich, {"legs": BULL_PUT})
    # with the shares in the account, a call spread's short leg is a covered call
    holder = acct(ex, "cash", "20000")
    ex.submit(holder, {"symbol": "ABC", "side": "buy", "qty": 100})
    assert ex.submit(holder, {"legs": BULL_CALL})[0].status == "filled"


def test_limit_spread_waits_and_then_fills_every_leg_at_or_better_than_the_limit():
    ex, feed, clock = make_exchange()
    a = acct(ex, "margin", "1000")
    now_net = market_net(ex, BULL_CALL)
    limit = (now_net - D("0.40")).quantize(D("0.01"))
    [o] = ex.submit(a, {"legs": BULL_CALL, "qty": 2, "type": "limit", "limit_price": str(limit), "price_effect": "debit",
                        "tif": "gtc"})
    assert o.status == "working" and o.limit_price == limit and not ex.positions(a) and not ex.account(a).fills
    assert ex.summary(a)["cash"] == D("1000")
    assert ex.summary(a)["option_buying_power"] == D("1000") - limit * 200 - D("2.60")  # reserved while it works
    move(ex, feed, clock, "ABC", "99.50")
    assert o.status == "working" and not ex.account(a).fills
    move(ex, feed, clock, "ABC", "99")
    assert o.status == "filled"
    f = {x.symbol: x for x in ex.account(a).fills}
    assert set(f) == {C100, C105} and f[C100].qty == f[C105].qty == 2
    paid = f[C100].price - f[C105].price
    assert paid <= limit and o.avg_fill_price == paid
    assert ex.summary(a)["cash"] == D("1000") - paid * 200 - D("2.60")
    assert all(sum(v for _, v in t.postings) == 0 for t in ex.account(a).ledger.transactions)


def test_credit_limit_and_cancel_and_day_expiry():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    credit_now = -market_net(ex, BULL_PUT)
    ask_for = (credit_now + D("0.50")).quantize(D("0.01"))
    [o] = ex.submit(a, {"legs": BULL_PUT, "type": "limit", "limit_price": str(ask_for), "price_effect": "credit"})
    assert o.status == "working" and o.limit_price == -ask_for and o.qty == 1
    with pytest.raises(OrderRejected, match="cannot be changed"):
        ex.replace(a, o.id, {"limit_price": "0.10"})
    [fok] = ex.submit(a, {"legs": BULL_PUT, "type": "limit", "limit_price": str(ask_for), "price_effect": "credit", "tif": "fok"})
    assert fok.status == "canceled" and not ex.account(a).fills
    move(ex, feed, clock, "ABC", "100", seconds=86400)
    assert o.status == "expired" and not ex.positions(a)
    [g] = ex.submit(a, {"legs": BULL_PUT, "type": "limit", "limit_price": str(ask_for), "price_effect": "credit", "tif": "gtc"})
    ex.cancel(a, g.id)
    move(ex, feed, clock, "ABC", "80")
    assert g.status == "canceled" and not ex.positions(a)


def test_covered_call_and_protective_put_with_a_stock_leg():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "20000")
    [o] = ex.submit(a, {"legs": [leg("ABC", "buy", 100), leg(C105, "sell")], "qty": 1})
    assert o.status == "filled" and o.fees == D("0.65")
    assert {p["symbol"]: p["qty"] for p in ex.positions(a)} == {"ABC": 100, C105: -1}
    assert ex.summary(a)["cash"] == D("20000") - D("10000") + ex.quote(C105).bid * 100 - D("0.65")
    [u] = ex.submit(a, {"legs": [leg("ABC", "sell", 100), leg(C105, "buy")], "qty": 1})  # unwind as one order
    assert u.status == "filled" and not ex.positions(a)
    [p] = ex.submit(a, {"legs": [leg("ABC", "buy", 50), leg(P95, "buy")], "qty": 1})
    assert p.status == "filled" and {x["symbol"]: x["qty"] for x in ex.positions(a)} == {"ABC": 50, P95: 1}


def test_working_multileg_order_expires_with_its_options_and_survives_a_restart(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = acct(ex)
    [o] = ex.submit(a, {"legs": BULL_CALL, "type": "limit", "limit_price": "0.05", "price_effect": "debit", "tif": "gtc"})
    far = [leg(opt("C", 100, FAR), "buy"), leg(opt("C", 105, FAR), "sell")]
    limit = (market_net(ex, far) - D("0.30")).quantize(D("0.01"))
    [keep] = ex.submit(a, {"legs": far, "type": "limit", "limit_price": str(limit), "price_effect": "debit", "tif": "gtc"})
    ex.save(force=True)
    feed2, clock2 = SimFeed(), SimClock(clock.now())
    ex2 = Exchange(feed2, clock2, data_dir=str(tmp_path))
    assert ex2.load()
    restored = ex2.engine.orders[keep.id]
    assert restored.legs == far and restored.limit_price == limit and restored.status == "working"
    feed2.set_quote("ABC", "99.99", "100", "100")
    ex2.tick()
    clock2.set(cal.close_ts(NEAR) - 5)
    move(ex2, feed2, clock2, "ABC", "100", seconds=10)
    assert ex2.engine.orders[o.id].status == "expired" and ex2.engine.orders[o.id].reason == "option expired"
    assert restored.status == "working"
    move(ex2, feed2, clock2, "ABC", "95")
    assert restored.status == "filled" and len(ex2.account(a).fills) == 2


def test_multileg_validation_messages():
    ex, _, _ = make_exchange(ABC="100.00", XYZ="50.00")
    a = acct(ex)
    base = {"legs": BULL_CALL, "qty": 1}
    bad = [({"legs": [leg(C100, "buy")]}, "two to four legs"),
           ({"legs": [leg(C100, "buy")] * 5}, "two to four legs"),
           ({"legs": "x"}, "two to four legs"),
           (dict(base, qty=0), "whole number from 1 to 10,000"),
           (dict(base, qty="1.5"), "whole number"),
           (dict(base, qty="many"), "invalid number"),
           (dict(base, type="stop"), "market or limit orders"),
           (dict(base, tif="forever"), "unknown time in force"),
           (dict(base, stop_loss="1"), "without stops or brackets"),
           (dict(base, extended_hours=True), "regular session only"),
           (dict(base, type="limit"), "net limit price is required"),
           (dict(base, type="limit", limit_price="1.50"), "price_effect must be debit"),
           (dict(base, type="limit", limit_price="-1.50", price_effect="debit"), "cannot be negative"),
           (dict(base, type="limit", limit_price="1.505", price_effect="debit"), "too many decimal places"),
           (dict(base, tif="gtd"), "good-till date is required"),
           ({"legs": [leg(C100, "buy"), leg(C100, "sell")]}, "appears in more than one leg"),
           ({"legs": [leg(C100, "buy"), leg(opt("C", 50, underlying="XYZ"), "sell")]}, "same underlying"),
           ({"legs": [leg(C100, "buy"), leg("XYZ", "sell", 100)]}, "same underlying"),
           ({"legs": [leg(C100, "buy"), leg("NOPE", "sell")]}, "unknown symbol NOPE"),
           ({"legs": [leg(C100, "buy"), leg(C105, "hold")]}, "each leg needs a side"),
           ({"legs": [leg(C100, "buy"), leg(C105, "sell", 0)]}, "option leg ratios"),
           ({"legs": [leg(C100, "buy"), leg(C105, "sell", "1.5")]}, "option leg ratios"),
           ({"legs": [leg(C100, "buy"), leg("ABC", "sell", "0.5")]}, "stock leg ratio"),
           ({"legs": [leg(C100, "buy"), leg(opt("C", 100, cal.previous_trading_day(cal.ny_date(T0))), "sell")]}, "expired"),
           (dict(base, oco=[{}, {}]), "cannot be linked"),
           ({"oco": [base, base]}, "a linked order cannot itself be multi-leg or linked"),
           ({"symbol": "ABC", "side": "buy", "qty": 1, "triggers": [base]}, "cannot itself be multi-leg or linked"),
           ({"symbol": "ABC", "side": "buy", "qty": 1, "triggers": [{"oco": [base]}]}, "cannot itself be multi-leg or linked")]
    for req_, msg in bad:
        with pytest.raises(OrderRejected, match=msg):
            ex.submit(a, req_)
        if "legs" in req_:
            with pytest.raises(OrderRejected, match=msg):
                ex.preview(a, req_)
    assert not ex.orders(a)
    # a zero net limit needs no price effect; client ids are idempotent
    [even] = ex.submit(a, dict(base, type="limit", limit_price="0", client_id="m1"))
    assert even.limit_price == 0 and even.status == "working"
    assert ex.submit(a, dict(base, type="limit", limit_price="0", client_id="m1")) == [even]


def test_naked_structures_need_a_margin_account_and_enough_buying_power():
    ex, _, _ = make_exchange()
    cash, small, big = acct(ex, "cash", "50000"), acct(ex, "margin", "1000"), acct(ex)
    strangle = {"legs": [leg(C105, "sell"), leg(P95, "sell")], "qty": 1}
    with pytest.raises(OrderRejected, match="uncovered calls need a margin account"):
        ex.submit(cash, strangle)
    with pytest.raises(OrderRejected, match="insufficient buying power"):
        ex.submit(small, strangle)
    assert ex.submit(big, strangle)[0].status == "filled"
    # the same account can afford the defined-risk version
    assert ex.submit(small, {"legs": CONDOR, "qty": 1})[0].status == "filled"
    s = ex.summary(small)
    assert D(0) < s["initial_requirement"] <= D("500")


# ---- exchange: preview ------------------------------------------------------------------------------
def test_preview_reports_price_payoff_margin_and_fees():
    ex, _, _ = make_exchange()
    a = acct(ex)
    pre = ex.preview_multileg(a, {"legs": BULL_CALL, "qty": 3})
    debit = market_net(ex, BULL_CALL)
    assert pre["ok"] and pre["net_price"] == debit and pre["price_effect"] == "debit" and pre["marketable"]
    assert pre["estimated_cost"] == debit * 300 and pre["estimated_fees"] == D("3.90")
    assert pre["estimated_total"] == debit * 300 + D("3.90")
    assert pre["max_loss"] == debit * 300 and pre["max_profit"] == (5 - debit) * 300
    assert pre["break_evens"] == [100 + debit] and not pre["max_loss_unlimited"] and pre["simulated"]
    assert [(l["symbol"], l["side"], l["qty"], l["position_after"]) for l in pre["legs"]] == [
        (C100, "buy", 3, 3), (C105, "sell", 3, -3)]
    assert pre["legs"][0]["estimated_price"] == ex.quote(C100).ask and pre["legs"][1]["estimated_price"] == ex.quote(C105).bid
    assert pre["requirement_before"] == 0 and pre["buying_power_after"] < pre["buying_power_before"]
    assert not ex.orders(a) and not ex.positions(a)  # a preview places nothing
    # the margin effect is the requirement of the position the order would create
    ex.submit(a, {"legs": BULL_CALL, "qty": 3})
    assert ex.summary(a)["initial_requirement"] == pre["margin_effect"] == pre["requirement_after"]
    assert ex.preview(a, {"legs": BULL_CALL, "qty": 3}).keys() == pre.keys()  # the general route accepts it too

    condor = ex.preview_multileg(a, {"legs": CONDOR, "qty": 2, "type": "limit", "limit_price": "1.00", "price_effect": "credit"})
    assert condor["price_effect"] == "credit" and condor["net_price"] == D("1.00")
    assert condor["max_profit"] == D("200.00") and condor["max_loss"] == D("800.00")
    assert condor["break_evens"] == [D("94.00"), D("106.00")]
    assert condor["marketable"] == (-market_net(ex, CONDOR) >= D("1.00"))
    assert condor["estimated_cost"] == D("-200.00") and condor["estimated_fees"] == D("5.20")

    strangle = ex.preview_multileg(a, {"legs": [leg(C105, "sell"), leg(P95, "sell")]})
    assert strangle["max_loss_unlimited"] and strangle["max_loss"] is None and strangle["ok"]
    assert "not limited" in " ".join(strangle["warnings"])
    cal_ = ex.preview_multileg(a, {"legs": [leg(C100, "sell"), leg(opt("C", 100, FAR), "buy")]})
    assert not cal_["payoff_at_expiry"] and cal_["max_profit"] is None and cal_["max_loss"] == cal_["estimated_cost"]
    closing = ex.preview_multileg(a, {"legs": [leg(C100, "sell"), leg(C105, "buy")], "qty": 3})
    assert closing["closing"] and closing["ok"] and closing["margin_effect"] == -pre["margin_effect"]
    poor = acct(ex, "cash", "100")
    refused = ex.preview_multileg(poor, {"legs": BULL_PUT})
    assert not refused["ok"] and "secure short options" in refused["reason"]


def test_multileg_api_routes():
    import time
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()))
    aid = ex.create_account("Margin", "margin").id
    ex.tick()
    c = TestClient(create_app(ex, run_loop=False))
    spot = float(ex.quote("NVLX").last)
    exp = chain.expirations(cal.ny_date(ex.now()))[2]
    ks = [k for k in chain.strikes(spot) if float(k) >= spot][:2]
    legs = [leg(build("NVLX", exp, "C", ks[0]), "buy"), leg(build("NVLX", exp, "C", ks[1]), "sell")]
    pre = c.post(f"/api/accounts/{aid}/multileg/preview", json={"legs": legs, "qty": 2})
    assert pre.status_code == 200
    pre = pre.json()
    assert pre["ok"] and pre["price_effect"] == "debit" and pre["max_loss"] == pytest.approx(pre["net_price"] * 200)
    assert pre["estimated_fees"] == 2.6 and len(pre["break_evens"]) == 1 and len(pre["legs"]) == 2
    r = c.post(f"/api/accounts/{aid}/multileg", json={"legs": legs, "qty": 2})
    assert r.status_code == 200
    row = r.json()
    assert row["status"] == "filled" and row["type"] == "multileg" and row["symbol"] == "NVLX"
    assert [l["side"] for l in row["legs"]] == ["buy", "sell"] and "Call" in row["legs"][0]["description"]
    assert len(c.get(f"/api/accounts/{aid}/positions").json()) == 2
    assert len(c.get(f"/api/accounts/{aid}/fills").json()) == 2
    # the general order route takes the same body
    r = c.post(f"/api/accounts/{aid}/orders", json={"legs": legs, "qty": 1, "type": "limit", "limit_price": 0.01,
                                                    "price_effect": "debit", "tif": "gtc"})
    assert r.status_code == 200 and r.json()[0]["status"] == "working" and r.json()[0]["legs"] == legs
    assert c.post(f"/api/accounts/{aid}/orders/preview", json={"legs": legs}).json()["max_loss"] > 0
    bad = c.post(f"/api/accounts/{aid}/multileg", json={"legs": legs[:1]})
    assert bad.status_code == 422 and bad.json()["rejected"] and "two to four legs" in bad.json()["detail"]
    assert c.post(f"/api/accounts/{aid}/multileg", json={"qty": 1}).status_code == 400
    assert c.post("/api/accounts/NOPE/multileg/preview", json={"legs": legs}).status_code == 404


# ---- expiry of spreads ----------------------------------------------------------------------------
def at_expiry(ex, feed, clock, price, expiry=NEAR):
    clock.set(cal.close_ts(expiry) - 5)
    move(ex, feed, clock, "ABC", price, seconds=10)


def open_spread(legs, qty=2, type="margin", cash="100000"):
    ex, feed, clock = make_exchange()
    a = acct(ex, type, cash)
    [o] = ex.submit(a, {"legs": legs, "qty": qty})
    assert o.status == "filled"
    return ex, feed, clock, a, ex.summary(a)["cash"]


def held(ex, a):
    return {p["symbol"]: p["qty"] for p in ex.positions(a)}


def test_vertical_with_both_legs_in_the_money_nets_to_cash():
    ex, feed, clock, a, cash = open_spread(BULL_CALL)
    at_expiry(ex, feed, clock, "120")
    assert held(ex, a) == {}  # exercised into 200 shares at 100 and assigned out of them at 105
    assert ex.summary(a)["cash"] == cash + D("1000")
    kinds = sorted(f.kind for f in ex.account(a).fills[-4:])
    assert kinds == ["assignment", "assignment", "exercise", "exercise"]
    assert ex.summary(a)["equity"] == ex.summary(a)["cash"] and ex.summary(a)["initial_requirement"] == 0


def test_vertical_pinned_between_strikes_leaves_stock():
    ex, feed, clock, a, cash = open_spread(BULL_CALL)
    at_expiry(ex, feed, clock, "103")
    assert held(ex, a) == {"ABC": 200}  # the long call is exercised; the short call expires worthless
    assert ex.summary(a)["cash"] == cash - D("20000")
    [p] = ex.positions(a)
    assert p["avg_cost"] == D("100")


def test_vertical_out_of_the_money_expires_worthless():
    ex, feed, clock, a, cash = open_spread(BULL_CALL, cash="1000")
    at_expiry(ex, feed, clock, "99")
    assert held(ex, a) == {} and ex.summary(a)["cash"] == cash
    assert [f.kind for f in ex.account(a).fills[-2:]] == ["expiration", "expiration"]


def test_put_credit_spread_at_expiry():
    ex, feed, clock, a, cash = open_spread([leg(P100, "sell"), leg(P95, "buy")])
    at_expiry(ex, feed, clock, "97")  # pinned: assigned 200 shares at 100, the long put is worthless
    assert held(ex, a) == {"ABC": 200} and ex.summary(a)["cash"] == cash - D("20000")
    ex, feed, clock, a, cash = open_spread([leg(P100, "sell"), leg(P95, "buy")])
    at_expiry(ex, feed, clock, "90")  # both in the money: lose the width
    assert held(ex, a) == {} and ex.summary(a)["cash"] == cash - D("1000")
    ex, feed, clock, a, cash = open_spread([leg(P100, "sell"), leg(P95, "buy")])
    at_expiry(ex, feed, clock, "101")
    assert held(ex, a) == {} and ex.summary(a)["cash"] == cash


def test_iron_condor_butterfly_and_calendar_at_expiry():
    ex, feed, clock, a, cash = open_spread(CONDOR, qty=1)
    at_expiry(ex, feed, clock, "100")
    assert held(ex, a) == {} and ex.summary(a)["cash"] == cash  # keeps the whole credit
    ex, feed, clock, a, cash = open_spread(CONDOR, qty=1)
    at_expiry(ex, feed, clock, "112")  # through the call wing: lose its 5.00 width, put wing worthless
    assert held(ex, a) == {} and ex.summary(a)["cash"] == cash - D("500")
    ex, feed, clock, a, cash = open_spread(BUTTERFLY, qty=1)
    at_expiry(ex, feed, clock, "120")  # everything in the money: +100 at 95, -200 at 100, +100 at 105
    assert held(ex, a) == {} and ex.summary(a)["cash"] == cash
    ex, feed, clock, a, cash = open_spread(BUTTERFLY, qty=1)
    at_expiry(ex, feed, clock, "100")  # body exactly at the money expires; the low wing is exercised
    assert held(ex, a) == {"ABC": 100} and ex.summary(a)["cash"] == cash - D("9500")
    far = opt("C", 100, FAR)
    ex, feed, clock, a, cash = open_spread([leg(C100, "sell"), leg(far, "buy")], qty=1)
    at_expiry(ex, feed, clock, "99")
    assert held(ex, a) == {far: 1} and ex.summary(a)["cash"] == cash
    at_expiry(ex, feed, clock, "110", FAR)
    assert held(ex, a) == {"ABC": 100} and ex.summary(a)["cash"] == cash - D("10000")


def test_covered_call_assigned_at_expiry_delivers_the_shares():
    ex, feed, clock, a, cash = open_spread([leg("ABC", "buy", 100), leg(C105, "sell")], qty=1, type="cash", cash="20000")
    at_expiry(ex, feed, clock, "110")
    assert held(ex, a) == {} and ex.summary(a)["cash"] == cash + D("10500")


# ---- exchange property: random multi-leg trading keeps the books straight ------------------------------
EX_SYMS = [opt(k, s, e) for k in "CP" for s in (90, 95, 100, 105, 110) for e in (NEAR, FAR)]


@st.composite
def exchange_script(draw):
    steps = []
    for _ in range(draw(st.integers(1, 10))):
        if draw(st.integers(0, 2)) == 0:
            steps.append(("move", Decimal(draw(st.integers(8500, 11500))) / 100))
            continue
        symbols = draw(st.lists(st.sampled_from(EX_SYMS + ["ABC"]), min_size=2, max_size=4, unique=True))
        if all(s == "ABC" for s in symbols):
            continue
        legs = [leg(s, draw(st.sampled_from(["buy", "sell"])), 100 if s == "ABC" else draw(st.integers(1, 2))) for s in symbols]
        req_ = {"legs": legs, "qty": draw(st.integers(1, 3)), "tif": draw(st.sampled_from(["day", "gtc", "fok"]))}
        offset = draw(st.one_of(st.none(), st.integers(-150, 150)))
        steps.append(("order", req_, offset))
    return steps


@settings(max_examples=60, deadline=None)
@given(exchange_script(), st.sampled_from(["margin", "cash"]))
def test_random_multileg_trading_keeps_ledger_and_fills_consistent(steps, account_type):
    ex, feed, clock = make_exchange()
    start = D("250000")
    a = acct(ex, account_type, str(start))
    for step_ in steps:
        if step_[0] == "move":
            move(ex, feed, clock, "ABC", step_[1])
            continue
        _, req_, offset = step_
        priced = net_price(req_["legs"], {l["symbol"]: ex.quote(l["symbol"]) for l in req_["legs"]})
        if offset is not None and priced is not None:
            limit = (priced[0] + Decimal(offset) / 100).quantize(D("0.01"))
            req_ = dict(req_, type="limit", limit_price=str(abs(limit)), price_effect="debit" if limit > 0 else "credit")
        try:
            ex.submit(a, req_)
        except OrderRejected:
            pass
        assert ex.summary(a)["equity"] == margin.equity(ex.account(a).cash, ex.account(a).positions(),
                                                        ex._marks(ex.account(a).positions()))
    account = ex.account(a)
    expected_pos = {}
    for o in ex.orders(a):
        mine = [f for f in account.fills if f.order_id == o.id]
        if o.status != "filled":
            assert not mine and o.filled_qty == 0  # never legged in
            continue
        assert [(f.symbol, f.side, f.qty) for f in mine] == [(l["symbol"], l["side"], o.qty * l["ratio"]) for l in o.legs]
        assert len({f.ts for f in mine}) == 1
        net = signed_net(o.legs, {f.symbol: f.price for f in mine})
        assert o.limit_price is None or net <= o.limit_price
        assert o.avg_fill_price == net.quantize(D("0.0001"))
        assert o.fees == sum((f.fee for f in mine), D(0))
        for f in mine:
            expected_pos[f.symbol] = expected_pos.get(f.symbol, D(0)) + (f.qty if f.side == "buy" else -f.qty)
    assert {s: n for s, n in expected_pos.items() if n} == account.positions()
    led = account.ledger
    assert all(sum(v for _, v in t.postings) == 0 for t in led.transactions)
    assert sum(led.balances.values()) == 0
    assert {k: v for k, v in led.recompute().items() if v} == {k: v for k, v in led.balances.items() if v}
    symbols = {f.symbol for f in account.fills}
    for s in symbols:
        assert led.balance(position_account(s)) == account.cost_basis(s)
    assert account.cash == start + account.realized - account.fees_paid - sum((account.cost_basis(s) for s in symbols), D(0))
    cash_moved = sum(((-1 if f.side == "buy" else 1) * f.qty * f.price * multiplier(f.symbol) - f.fee for f in account.fills), D(0))
    assert account.cash == start + cash_moved
    if account_type == "cash":
        assert all(n > 0 for s, n in account.positions().items() if not parse(s))  # never short stock
