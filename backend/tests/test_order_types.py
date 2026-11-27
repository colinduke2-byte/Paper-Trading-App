"""On-open and on-close orders, good-till-date, standalone one-cancels-other and one-triggers-other."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from hypothesis import given, settings, strategies as st

from app.core import calendar as cal
from app.core.clock import SimClock
from app.exchange import Exchange, OrderRejected
from app.marketdata.feed import SimFeed
from app.matching.engine import MatchingEngine
from helpers import D, T0, fills, make_exchange, move, order, q
from test_matching import price, quote_tape

TO_MIDNIGHT = 86400 - T0 % 86400  # seconds from the test clock's start to the next UTC day


def acct(ex, type="margin", cash="100000"):
    return ex.create_account("t", type, D(cash)).id


def ny(*args):
    return datetime(*args, tzinfo=cal.NY).timestamp()


# ---- engine: auctions ----------------------------------------------------------
def test_on_open_and_on_close_orders_only_trade_in_their_auction():
    e = MatchingEngine()
    moo, moc = order("buy", 100, "market_on_open"), order("sell", 50, "market_on_close")
    assert not fills(e.submit(moo, q(99.99, 100.00), 1)) and not fills(e.submit(moc, q(99.99, 100.00), 1))
    assert not fills(e.on_quote(q(101.99, 102.00), 2)) and moo.status == moc.status == "working"
    f = fills(e.on_auction("open", lambda s: q(102.40, 102.60, last=102.50), 3))
    assert [(x.order_id, x.qty, x.price) for x in f] == [(moo.id, 100, D("102.50"))]
    assert moo.status == "filled" and moc.status == "working"
    f = fills(e.on_auction("close", lambda s: q(103.90, 104.10, last=104.00), 4))
    assert [(x.order_id, x.qty, x.price) for x in f] == [(moc.id, 50, D("104.00"))]
    assert not e.on_auction("open", lambda s: q(1, 2), 5) and not e.on_auction("close", lambda s: q(1, 2), 5)


def test_limit_on_open_and_close_need_the_auction_price_inside_the_limit():
    e = MatchingEngine()
    buy_ok = order("buy", 10, "limit_on_open", limit_price=100)
    buy_no = order("buy", 10, "limit_on_open", limit_price=99)
    sell_ok = order("sell", 10, "limit_on_close", limit_price=100)
    sell_no = order("sell", 10, "limit_on_close", limit_price=101)
    for o in (buy_ok, buy_no, sell_ok, sell_no):
        e.submit(o, q(99.99, 100.00), 1)
    e.on_auction("open", lambda s: q(99.40, 99.60, last=99.50), 2)
    assert buy_ok.status == "filled" and buy_ok.avg_fill_price == D("99.50")  # better than the limit
    assert buy_no.status == "expired" and "not reached at the open" in buy_no.reason and buy_no.filled_qty == 0
    e.on_auction("close", lambda s: q(100.40, 100.60, last=100.50), 3)
    assert sell_ok.status == "filled" and sell_ok.avg_fill_price == D("100.50")
    assert sell_no.status == "expired" and "not reached at the close" in sell_no.reason


def test_auction_without_a_price_expires_the_order():
    e = MatchingEngine()
    o = order("buy", 10, "market_on_open")
    e.submit(o, None, 1)
    e.on_auction("open", lambda s: None, 2)
    assert o.status == "expired" and "no price" in o.reason


# ---- engine: standalone one-cancels-other ----------------------------------------
def test_oco_first_fill_cancels_the_rest():
    e = MatchingEngine()
    a = order("sell", 100, "limit", limit_price=110, tif="gtc")
    b = order("sell", 100, "stop", stop_price=95, tif="gtc")
    c = order("sell", 100, "limit", limit_price=120, tif="gtc")
    e.submit_oco([a, b, c], {"ABC": q(99.99, 100.00)}, 1)
    assert a.status == b.status == c.status == "working" and a.oco_group == b.oco_group == c.oco_group
    e.on_quote(q(94.90, 95.00, last=94.95), 2)
    assert b.status == "filled" and a.status == c.status == "canceled"
    assert not fills(e.on_quote(q(125, 126, last=126), 3))


def test_oco_partial_fill_reduces_the_others_by_the_same_quantity():
    e = MatchingEngine()
    a = order("sell", 100, "limit", limit_price=100, tif="gtc")
    b = order("sell", 100, "stop", stop_price=95, tif="gtc")
    small = order("sell", 30, "limit", limit_price=120, tif="gtc")
    e.submit_oco([a, b, small], {"ABC": q(100.00, 100.01, bid_size=40)}, 1)
    assert a.filled_qty == 40 and b.qty == 60 and b.status == "working"
    assert small.status == "canceled"  # the 40 filled elsewhere is more than all it wanted
    e.on_quote(q(94.00, 94.10, last=94.05), 2)  # the stop sells the last 60; the limit is then done too
    assert b.status == "filled" and b.filled_qty == 60 and a.status == "canceled" and a.filled_qty == 40


def test_oco_immediate_fill_at_submission_cancels_later_orders_unprocessed():
    e = MatchingEngine()
    a, b = order("buy", 10), order("buy", 10)
    ev = e.submit_oco([a, b], {"ABC": q(99.99, 100.00)}, 1)
    assert a.status == "filled" and b.status == "canceled" and len(fills(ev)) == 1


def test_oco_across_symbols():
    e = MatchingEngine()
    a = order("buy", 10, "limit", limit_price=50, tif="gtc", symbol="ABC")
    b = order("buy", 20, "limit", limit_price=20, tif="gtc", symbol="XYZ")
    e.submit_oco([a, b], {"ABC": q(59.99, 60.00), "XYZ": q(24.99, 25.00, symbol="XYZ")}, 1)
    e.on_quote(q(19.90, 19.95, symbol="XYZ"), 2)
    assert b.status == "filled" and a.status == "canceled"


# ---- engine: one-triggers-other ---------------------------------------------------
def test_triggered_child_with_its_own_quantity_needs_a_complete_parent_fill():
    e = MatchingEngine()
    parent = order("buy", 500, "limit", limit_price=100)
    child = order("buy", 70, "limit", limit_price=20, symbol="XYZ", role="triggered", tif="gtc")
    e.submit_linked(parent, [child], q(99.99, 100.00, ask_size=200), 1, oco=False)
    assert parent.filled_qty == 200 and child.status == "held" and child.oco_group is None
    assert not fills(e.on_quote(q(19.00, 19.10, symbol="XYZ"), 2))  # held orders never trade
    e.on_quote(q(99.99, 100.00, ask_size=300, trade=False), 3)
    assert parent.status == "filled" and child.status == "working" and child.qty == 70
    assert fills(e.on_quote(q(19.00, 19.10, symbol="XYZ"), 4))[0].qty == 70

    parent2 = order("buy", 500, "limit", limit_price=100)
    child2 = order("buy", 70, "limit", limit_price=20, symbol="XYZ", role="triggered", tif="gtc")
    e.submit_linked(parent2, [child2], q(99.99, 100.00, ask_size=200), 5, oco=False)
    e.cancel(parent2.id, 6)
    assert child2.status == "canceled" and "did not fill completely" in child2.reason


def test_exit_children_without_oco_both_work_and_bracket_is_unchanged():
    e = MatchingEngine()
    parent = order("buy", 100)
    exit_ = order("sell", 100, "limit", limit_price=110, tif="gtc", role="exit")
    e.submit_linked(parent, [exit_], q(99.99, 100.00), 1, oco=False)
    assert exit_.status == "working" and exit_.qty == 100
    p2, tp, sl = order("buy", 100), order("sell", 100, "limit", limit_price=110), order("sell", 100, "stop", stop_price=95)
    e.submit_bracket(p2, [tp, sl], q(99.99, 100.00), 2)
    assert tp.oco_group == sl.oco_group == p2.id and tp.status == "working"


# ---- engine properties --------------------------------------------------------------
@st.composite
def auction_order(draw):
    typ = draw(st.sampled_from(["market_on_open", "market_on_close", "limit_on_open", "limit_on_close"]))
    kw = {"limit_price": draw(price)} if typ.startswith("limit") else {}
    return order(draw(st.sampled_from(["buy", "sell"])), draw(st.integers(1, 3000)), typ, **kw)


@settings(max_examples=300, deadline=None)
@given(auction_order(), quote_tape(), st.lists(st.sampled_from(["quote", "open", "close"]), min_size=1, max_size=25))
def test_auction_order_invariants(o, tape, script):
    e = MatchingEngine()
    all_fills = fills(e.submit(o, tape[0], 0))
    assert not all_fills
    mine = "open" if o.type.endswith("open") else "close"
    for i, (quote, step) in enumerate(zip(tape, script), start=1):
        before = o.status
        ev = e.on_quote(quote, i) if step == "quote" else e.on_auction(step, lambda s: quote, i)
        new = fills(ev)
        if step != mine or before != "working":
            assert not new and o.status == before  # only its own auction can touch it, and only once
        else:
            assert o.status in ("filled", "expired")
            assert all(f.price == quote.last for f in new)
        all_fills += new
    assert sum((f.qty for f in all_fills), Decimal(0)) == o.filled_qty
    assert o.filled_qty in (0, o.qty)  # an auction fills in full or not at all
    if o.limit_price is not None:
        for f in all_fills:
            assert f.price <= o.limit_price if o.side == "buy" else f.price >= o.limit_price
    assert (o.status == "filled") == (o.filled_qty == o.qty)


@st.composite
def oco_orders(draw):
    side = draw(st.sampled_from(["buy", "sell"]))
    out = []
    for _ in range(draw(st.integers(2, 4))):
        typ = draw(st.sampled_from(["market", "limit", "stop", "stop_limit"]))
        kw = {"tif": "gtc"}
        if typ in ("limit", "stop_limit"):
            kw["limit_price"] = draw(price)
        if typ in ("stop", "stop_limit"):
            kw["stop_price"] = draw(price)
        out.append(order(side, draw(st.integers(1, 2000)), typ, **kw))
    return out


@settings(max_examples=300, deadline=None)
@given(oco_orders(), quote_tape())
def test_oco_group_never_fills_more_than_its_largest_order(orders, tape):
    e = MatchingEngine()
    original = {o.id: o.qty for o in orders}
    all_fills = fills(e.submit_oco(orders, {"ABC": tape[0]}, 0))
    for i, quote in enumerate(tape[1:], start=1):
        all_fills += fills(e.on_quote(quote, i))
    total = sum((f.qty for f in all_fills), Decimal(0))
    assert total == sum((o.filled_qty for o in orders), Decimal(0)) <= max(original.values())
    for o in orders:
        assert o.filled_qty <= o.qty <= original[o.id]
        if o.limit_price is not None:
            for f in (f for f in all_fills if f.order_id == o.id):
                assert f.price <= o.limit_price if o.side == "buy" else f.price >= o.limit_price
    if any(o.status == "filled" and o.qty == original[o.id] for o in orders):
        assert not [o for o in orders if o.status in ("working", "partially_filled")]


# ---- exchange: session mode 'always' (open and close are the UTC day rollover) -----------
def test_always_mode_on_close_and_on_open_fill_at_the_utc_day_rollover():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    [moc] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "market_on_close"})
    [moo] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "market_on_open"})
    [loc] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit_on_close", "limit_price": "100"})
    assert moc.tif == "cls" and moo.tif == "opg" and moc.status == moo.status == "working"
    move(ex, feed, clock, "ABC", "101", seconds=TO_MIDNIGHT - 5)  # last price of the day
    assert moc.status == moo.status == "working" and not ex.positions(a)
    clock.advance(10)
    feed.set_quote("ABC", "102.99", "103.00", "103.00")  # first price of the new day
    ex.tick()
    assert moc.status == "filled" and moc.avg_fill_price == D("101")  # the close: last price of the old day
    assert moo.status == "filled" and moo.avg_fill_price == D("103.00")
    assert loc.status == "expired"
    # an on-open order entered now waits a whole day
    [late] = ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 20, "type": "limit_on_open", "limit_price": "90"})
    move(ex, feed, clock, "ABC", "104", seconds=3600)
    assert late.status == "working"
    move(ex, feed, clock, "ABC", "105", seconds=86400)
    assert late.status == "filled" and late.avg_fill_price == D("105") and not ex.positions(a)


def test_always_mode_close_uses_the_last_price_of_the_old_day():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    [moc] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "market_on_close"})
    move(ex, feed, clock, "ABC", "101", seconds=TO_MIDNIGHT - 5)
    clock.advance(10)
    ex.tick()  # no new quote: the closing price is the last one seen
    assert moc.status == "filled" and moc.avg_fill_price == D("101")
    assert ex.summary(a)["cash"] == D("100000") - D("1010")


# ---- exchange: real sessions ---------------------------------------------------------------
def us_exchange(start):
    clock, feed = SimClock(start), SimFeed()
    ex = Exchange(feed, clock, session_mode="us")
    feed.set_quote("ABC", "99.99", "100", "100")
    ex.tick()
    return ex, feed, clock, acct(ex)


def step(ex, feed, clock, when, px=None):
    clock.set(when)
    if px is not None:
        feed.set_quote("ABC", D(str(px)) - D("0.01"), str(px), str(px))
    ex.tick()


def test_us_sessions_on_open_fills_at_the_open_and_on_close_at_the_close():
    ex, feed, clock, a = us_exchange(ny(2026, 11, 22, 12, 0))  # Sunday
    [moo] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "market_on_open"})
    [loo] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit_on_open", "limit_price": "101"})
    [moc] = ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 10, "type": "market_on_close"})
    step(ex, feed, clock, ny(2026, 11, 23, 8, 0), 101.50)  # pre-market: nothing trades
    assert moo.status == loo.status == moc.status == "working"
    step(ex, feed, clock, ny(2026, 11, 23, 9, 30, 1), 102)
    assert moo.status == "filled" and moo.avg_fill_price == D("102")
    assert loo.status == "expired" and moc.status == "working"
    step(ex, feed, clock, ny(2026, 11, 23, 15, 59, 50), 104)
    assert moc.status == "working"
    step(ex, feed, clock, ny(2026, 11, 23, 16, 0, 1))
    assert moc.status == "filled" and moc.avg_fill_price == D("104") and not ex.positions(a)


def test_on_close_order_entered_after_the_close_waits_for_the_next_close():
    ex, feed, clock, a = us_exchange(ny(2026, 11, 23, 15, 0))  # Monday, regular session
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    step(ex, feed, clock, ny(2026, 11, 23, 16, 0, 1))
    [moc] = ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 10, "type": "market_on_close"})
    [moo] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 5, "type": "market_on_open"})
    step(ex, feed, clock, ny(2026, 11, 23, 20, 0, 1))  # the day ends: day orders expire, these do not
    assert moc.status == moo.status == "working"
    step(ex, feed, clock, ny(2026, 11, 24, 9, 30, 1), 98)
    assert moo.status == "filled" and moo.avg_fill_price == D("98") and moc.status == "working"
    step(ex, feed, clock, ny(2026, 11, 24, 12, 0), 97)
    step(ex, feed, clock, ny(2026, 11, 24, 16, 0, 1))
    assert moc.status == "filled" and moc.avg_fill_price == D("97")


# ---- good-till-date ------------------------------------------------------------------------------
def test_good_till_date_expires_at_the_end_of_that_utc_day_in_always_mode():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    today = datetime.fromtimestamp(T0, tz=timezone.utc).date()
    tomorrow = today.fromordinal(today.toordinal() + 1)
    base = {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "limit_price": "90", "tif": "gtd"}
    [g1] = ex.submit(a, dict(base, good_till_date=today.isoformat()))
    [g2] = ex.submit(a, dict(base, good_till_date=tomorrow.isoformat()))
    assert g1.good_till == today.isoformat() and g1.expire_at == T0 + TO_MIDNIGHT
    move(ex, feed, clock, "ABC", "100", seconds=TO_MIDNIGHT - 5)
    assert g1.status == g2.status == "working"
    move(ex, feed, clock, "ABC", "100", seconds=10)
    assert g1.status == "expired" and g2.status == "working"  # a day order would also have expired; g2 lives on
    move(ex, feed, clock, "ABC", "89", seconds=3600)
    assert g2.status == "filled"


def test_good_till_date_expires_at_the_regular_close_with_real_sessions():
    ex, feed, clock, a = us_exchange(ny(2026, 11, 23, 10, 0))
    base = {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "limit_price": "90", "tif": "gtd"}
    [g] = ex.submit(a, dict(base, good_till_date="2026-11-27"))  # the day after Thanksgiving closes at 13:00
    assert g.expire_at == ny(2026, 11, 27, 13, 0)
    step(ex, feed, clock, ny(2026, 11, 25, 20, 0, 1))
    step(ex, feed, clock, ny(2026, 11, 27, 12, 59))
    assert g.status == "working"
    step(ex, feed, clock, ny(2026, 11, 27, 13, 0, 1))
    assert g.status == "expired"
    for bad, msg in (("2026-11-26", "market is closed on 2026-11-26"), ("2026-11-28", "market is closed"),
                     ("2026-11-20", "already passed"), ("2028-01-03", "at most one year"),
                     ("next week", "must look like"), (None, "is required")):
        with pytest.raises(OrderRejected, match=msg):
            ex.submit(a, dict(base, good_till_date=bad))
    with pytest.raises(OrderRejected, match="needs time in force gtd"):
        ex.submit(a, dict(base, tif="day", good_till_date="2026-11-27"))


def test_good_till_date_bracket_entry_expiring_cancels_its_children():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    today = datetime.fromtimestamp(T0, tz=timezone.utc).date().isoformat()
    entry, tp, sl = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "limit_price": "90",
                                  "tif": "gtd", "good_till_date": today, "take_profit": "95", "stop_loss": "85"})
    move(ex, feed, clock, "ABC", "100", seconds=TO_MIDNIGHT + 5)
    assert entry.status == "expired" and tp.status == sl.status == "canceled"


# ---- validation messages --------------------------------------------------------------------------
def test_new_order_type_validation_messages():
    ex, _, _ = make_exchange()
    a = acct(ex)
    opt = "ABC261218C00100000"
    o = {"symbol": "ABC", "side": "buy", "qty": 1}
    lim = dict(o, type="limit", limit_price="90", tif="gtc")
    bad = [(dict(o, type="limit_on_open"), "limit price is required"),
           (dict(o, type="limit_on_close"), "limit price is required"),
           (dict(o, type="market_on_open", tif="gtc"), "take no time in force"),
           (dict(o, type="market_on_close", tif="ioc"), "take no time in force"),
           (dict(o, type="limit_on_open", limit_price="90", extended_hours=True), "extended hours"),
           (dict(o, type="market_on_open", symbol=opt), "stocks only"),
           (dict(o, type="market_on_open", qty="0.5"), "fractional"),
           (dict(o, tif="gtd"), "good-till date is required"),
           ({"oco": [lim]}, "oco needs at least 2 orders"),
           ({"oco": [lim] * 6}, "at most 5 orders"),
           ({"oco": "x"}, "must be a list of orders"),
           ({"oco": [lim, dict(lim, tif="ioc")]}, "one-cancels-other orders cannot be immediate-or-cancel"),
           ({"oco": [lim, dict(lim, take_profit="99")]}, "cannot also carry take profit"),
           ({"oco": [lim, dict(lim, symbol="NOPE")]}, "unknown symbol NOPE"),
           ({"oco": [lim, lim], "triggers": [lim]}, "either oco or triggers"),
           (dict(o, triggers=[]), "triggers needs at least 1 order"),
           (dict(o, triggers=[lim] * 5), "at most 5 orders"),
           (dict(o, tif="fok", triggers=[lim]), "one-triggers-other orders cannot be immediate-or-cancel"),
           (dict(o, triggers=[dict(lim, tif="day")]), "good-till-canceled or good-till-date"),
           (dict(o, triggers=[{"side": "buy", "type": "limit", "limit_price": "90"}]), "must close the entry"),
           (dict(o, triggers=[{"symbol": "XYZ", "type": "limit", "limit_price": "90"}]), "unknown symbol XYZ"),
           (dict(o, triggers=[{"type": "limit", "limit_price": "110"}, {"type": "stop", "stop_price": "90"}]),
            "must cancel each other"),
           (dict(o, take_profit="110", triggers=[lim]), "cannot also carry take profit"),
           ("nonsense", "must be a JSON object")]
    for req, msg in bad:
        with pytest.raises(OrderRejected, match=msg):
            ex.submit(a, req)
    assert not ex.orders(a)


# ---- exchange: one-cancels-other -------------------------------------------------------------------
def test_standalone_oco_closes_a_position_once():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "10000")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
    req = {"client_id": "oco-1", "oco": [
        {"symbol": "ABC", "side": "sell", "qty": 100, "type": "limit", "limit_price": "110", "tif": "gtc"},
        {"symbol": "ABC", "side": "sell", "qty": 100, "type": "stop", "stop_price": "95", "tif": "gtc"}]}
    pre = ex.preview(a, req)
    assert pre["ok"] and pre["closing"] and len(pre["linked_orders"]) == 1 and "One-cancels-other" in " ".join(pre["warnings"])
    target, stop = ex.submit(a, req)
    assert [o.id for o in ex.submit(a, req)] == [target.id, stop.id]  # same client id: nothing new
    assert target.oco_group == stop.oco_group and target.status == stop.status == "working"
    move(ex, feed, clock, "ABC", "94")
    assert stop.status == "filled" and target.status == "canceled" and not ex.positions(a)
    move(ex, feed, clock, "ABC", "111")
    assert not ex.positions(a) and len(ex.account(a).fills) == 2


def test_oco_entries_reserve_buying_power_once_per_symbol():
    ex, feed, clock = make_exchange(ABC="100.00", XYZ="50.00")
    a = acct(ex, "cash", "10000")
    leg = lambda px: {"symbol": "ABC", "side": "buy", "qty": 90, "type": "limit", "limit_price": px, "tif": "gtc"}
    ex.submit(a, {"oco": [leg("99"), leg("98")]})  # 8,910 at most, not 17,730
    assert ex.summary(a)["buying_power"] == D("1090.00")
    with pytest.raises(OrderRejected, match="insufficient cash"):
        # different symbols could both fill on the same tick, so both count
        ex.submit(a, {"oco": [{"symbol": "XYZ", "side": "buy", "qty": 21, "type": "limit", "limit_price": "49", "tif": "gtc"},
                              {"symbol": "ABC", "side": "buy", "qty": 20, "type": "limit", "limit_price": "5", "tif": "gtc"}]})


# ---- exchange: one-triggers-other ---------------------------------------------------------------------
def test_oto_with_exit_orders_behaves_like_a_bracket():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    entry, target, stop = ex.submit(a, {
        "symbol": "ABC", "side": "buy", "qty": 100, "type": "limit", "limit_price": "99", "tif": "gtc",
        "triggers_oco": True,
        "triggers": [{"type": "limit", "limit_price": "105"}, {"type": "trailing_stop", "trail_amount": "2"}]})
    assert target.status == stop.status == "held" and target.role == stop.role == "exit" and target.tif == "gtc"
    assert (target.symbol, target.side, target.qty) == ("ABC", "sell", 100)
    move(ex, feed, clock, "ABC", "106")
    assert target.status == "held" and not ex.positions(a)  # held orders do not trade
    move(ex, feed, clock, "ABC", "98.50", ask_size=40)
    assert entry.filled_qty == 40 and target.status == "held"
    ex.cancel(a, entry.id)
    assert target.status == stop.status == "working" and target.qty == stop.qty == 40
    move(ex, feed, clock, "ABC", "105.50")
    assert target.status == "filled" and stop.status == "canceled" and not ex.positions(a)


def test_oto_triggered_order_with_its_own_quantity():
    ex, feed, clock = make_exchange(ABC="100.00", XYZ="50.00")
    a = acct(ex, "cash", "10000")
    req = {"symbol": "ABC", "side": "sell", "qty": 50, "type": "limit", "limit_price": "101", "tif": "gtc",
           "triggers": [{"symbol": "XYZ", "side": "buy", "qty": 100, "type": "limit", "limit_price": "49"}]}
    with pytest.raises(OrderRejected, match="margin account"):
        ex.submit(a, req)  # nothing to sell yet
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 50})  # 5,000 left
    assert ex.preview(a, req)["ok"]  # the sale pays for most of the purchase it triggers
    with pytest.raises(OrderRejected, match="insufficient cash"):
        ex.submit(a, dict(req, triggers=[dict(req["triggers"][0], qty=300)]))
    entry, child = ex.submit(a, req)
    assert child.status == "held" and child.role == "triggered" and child.qty == 100
    # worst case is still "nothing fills": the held purchase can only follow the sale that pays for it
    assert ex.summary(a)["buying_power"] == D("5000")
    move(ex, feed, clock, "XYZ", "48")
    assert child.status == "held"
    move(ex, feed, clock, "ABC", "101.50", bid_size=50)
    assert entry.status == "filled" and child.status == "working"
    move(ex, feed, clock, "XYZ", "48.50")
    assert child.status == "filled" and {p["symbol"]: p["qty"] for p in ex.positions(a)} == {"XYZ": 100}

    entry2, child2 = ex.submit(a, {"symbol": "XYZ", "side": "sell", "qty": 100, "type": "limit", "limit_price": "60",
                                   "tif": "gtc", "triggers": [{"symbol": "ABC", "side": "buy", "qty": 10,
                                                               "type": "limit", "limit_price": "90"}]})
    ex.cancel(a, entry2.id)
    assert child2.status == "canceled" and "did not fill completely" in child2.reason


# ---- closing orders that would uncover a short option ----------------------------------------------------
def test_selling_the_shares_behind_a_covered_call_is_not_treated_as_closing():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "20000")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
    from test_exchange import _option
    call, _ = _option("C", "105")
    assert ex.submit(a, {"symbol": call, "side": "sell", "qty": 1})[0].status == "filled"
    with pytest.raises(OrderRejected, match="uncovered calls need a margin account"):
        ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 100})
    ex.submit(a, {"symbol": call, "side": "buy", "qty": 1})
    assert ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 100})[0].status == "filled"


def test_new_order_fields_survive_a_restart(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = acct(ex)
    today = datetime.fromtimestamp(T0, tz=timezone.utc).date().isoformat()
    [gtd] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "limit_price": "90",
                          "tif": "gtd", "good_till_date": today})
    [moc] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "market_on_close"})
    ex.save(force=True)
    feed2, clock2 = SimFeed(), SimClock(clock.now())
    ex2 = Exchange(feed2, clock2, data_dir=str(tmp_path))
    assert ex2.load()
    feed2.set_quote("ABC", "99.99", "100", "100")
    ex2.tick()
    move(ex2, feed2, clock2, "ABC", "100", seconds=TO_MIDNIGHT + 5)
    assert ex2.engine.orders[gtd.id].status == "expired" and ex2.engine.orders[gtd.id].expire_at == gtd.expire_at
    assert ex2.engine.orders[moc.id].status == "filled"
