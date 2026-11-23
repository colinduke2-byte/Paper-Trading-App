from decimal import Decimal

from hypothesis import given, settings, strategies as st

from app.core.models import Order
from app.matching.engine import MatchingEngine
from helpers import D, fills, order, q


def test_market_buy_fills_at_ask():
    e = MatchingEngine()
    o = order("buy", 100)
    ev = e.submit(o, q(99.99, 100.00), 1)
    assert o.status == "filled" and fills(ev)[0].price == D("100.0000")


def test_market_order_walks_the_book():
    e = MatchingEngine()
    o = order("buy", 250)
    e.submit(o, q(9.99, 10.00, ask_size=100), 1)
    # 100 @ 10.00, 100 @ 10.01, 50 @ 10.02
    assert o.avg_fill_price == D("10.0080")
    s = order("sell", 250)
    e.submit(s, q(9.99, 10.00, bid_size=100), 1)
    assert s.avg_fill_price == D("9.9820")


def test_limit_rests_then_fills_when_ask_reaches_it():
    e = MatchingEngine()
    o = order("buy", 100, "limit", limit_price=99.50)
    assert not fills(e.submit(o, q(99.99, 100.00), 1))
    assert not fills(e.on_quote(q(99.60, 99.61, last=99.61), 2))
    f = fills(e.on_quote(q(99.40, 99.45, last=99.45), 3))
    assert f[0].price == D("99.45") and o.status == "filled"   # price improvement, never worse than limit


def test_limit_partial_fills_by_displayed_size():
    e = MatchingEngine()
    o = order("buy", 500, "limit", limit_price=100)
    e.submit(o, q(99.99, 100.00, ask_size=200), 1)
    assert o.filled_qty == 200 and o.status == "partially_filled"
    e.on_quote(q(99.99, 100.00, ask_size=200, trade=False), 2)
    e.on_quote(q(99.99, 100.00, ask_size=200, trade=False), 3)
    assert o.filled_qty == 500 and o.status == "filled"


def test_resting_limit_fills_on_trade_through_only():
    e = MatchingEngine()
    o = order("sell", 100, "limit", limit_price=101)
    e.submit(o, q(99.99, 100.00), 1)
    assert not fills(e.on_quote(q(100.90, 101.10, last=101.00, last_size=500), 2))  # touch is not enough
    f = fills(e.on_quote(q(100.95, 101.10, last=101.05, last_size=60), 3))
    assert f[0].price == D("101") and f[0].qty == 60


def test_stop_triggers_on_last_trade():
    e = MatchingEngine()
    o = order("sell", 100, "stop", stop_price=95)
    e.submit(o, q(99.99, 100.00), 1)
    assert not fills(e.on_quote(q(94.90, 95.20, last=95.10), 2))  # bid below stop but no qualifying print
    f = fills(e.on_quote(q(94.80, 94.90, last=94.95), 3))
    assert o.triggered and f[0].price == D("94.8000")


def test_stop_limit_rests_after_trigger():
    e = MatchingEngine()
    o = order("buy", 100, "stop_limit", stop_price=105, limit_price=105.10)
    e.submit(o, q(99.99, 100.00), 1)
    assert not fills(e.on_quote(q(105.40, 105.50, last=105.50), 2))  # triggered, but ask above limit
    assert o.triggered and o.status == "working"
    assert fills(e.on_quote(q(105.00, 105.05, last=105.05), 3))[0].price == D("105.05")


def test_trailing_stop_follows_the_high():
    e = MatchingEngine()
    o = order("sell", 10, "trailing_stop", trail_amount=2)
    e.submit(o, q(99.99, 100.00), 1)
    assert o.stop_price == D("98.00")
    e.on_quote(q(104.99, 105.00), 2)
    assert o.stop_price == D("103.00")
    e.on_quote(q(103.50, 103.60, last=103.60), 3)
    assert o.stop_price == D("103.00") and not o.triggered
    assert fills(e.on_quote(q(102.90, 103.00, last=103.00), 4))


def test_trailing_percent_buy():
    e = MatchingEngine()
    o = order("buy", 10, "trailing_stop", trail_percent=10)
    e.submit(o, q(99.99, 100.00), 1)
    assert o.stop_price == D("110.00")
    e.on_quote(q(89.99, 90.00), 2)
    assert o.stop_price == D("99.00")


def test_ioc_and_fok():
    e = MatchingEngine()
    ioc = order("buy", 500, "limit", limit_price=100, tif="ioc")
    e.submit(ioc, q(99.99, 100.00, ask_size=200), 1)
    assert ioc.filled_qty == 200 and ioc.status == "canceled"
    fok = order("buy", 500, "limit", limit_price=100, tif="fok")
    assert not fills(e.submit(fok, q(99.99, 100.00, ask_size=200), 1))
    assert fok.status == "canceled" and fok.filled_qty == 0
    fok2 = order("buy", 200, "limit", limit_price=100, tif="fok")
    e.submit(fok2, q(99.99, 100.00, ask_size=200), 1)
    assert fok2.status == "filled"


def test_session_rules():
    e = MatchingEngine()
    mkt = order("buy", 10)
    assert not fills(e.submit(mkt, q(99.99, 100.00), 1, "closed")) and mkt.status == "working"
    ext = order("buy", 10, "limit", limit_price=100, extended_hours=True)
    assert fills(e.submit(ext, q(99.99, 100.00), 1, "pre"))
    plain = order("buy", 10, "limit", limit_price=100)
    assert not fills(e.submit(plain, q(99.99, 100.00), 1, "pre"))
    assert fills(e.on_quote(q(99.99, 100.00), 2, "regular"))  # both queued orders fill at the open


def test_bracket_activates_children_and_oco_cancels():
    e = MatchingEngine()
    parent = order("buy", 100)
    tp = order("sell", 100, "limit", limit_price=110, tif="gtc")
    sl = order("sell", 100, "stop", stop_price=95, tif="gtc")
    e.submit_bracket(parent, [tp, sl], q(99.99, 100.00), 1)
    assert parent.status == "filled" and tp.status == "working" and sl.status == "working"
    e.on_quote(q(94.90, 95.00, last=94.95), 2)
    assert sl.status == "filled" and tp.status == "canceled"


def test_bracket_children_cancel_when_entry_never_fills():
    e = MatchingEngine()
    parent = order("buy", 100, "limit", limit_price=90)
    tp = order("sell", 100, "limit", limit_price=110)
    e.submit_bracket(parent, [tp], q(99.99, 100.00), 1)
    assert tp.status == "held"
    assert not fills(e.on_quote(q(110.00, 110.10, last=110.20), 2))  # held child must not trade
    e.cancel(parent.id, 3)
    assert tp.status == "canceled"


def test_partial_entry_cancel_sizes_children_to_filled_quantity():
    e = MatchingEngine()
    parent = order("buy", 500, "limit", limit_price=100)
    sl = order("sell", 500, "stop", stop_price=95)
    e.submit_bracket(parent, [sl], q(99.99, 100.00, ask_size=200), 1)
    e.cancel(parent.id, 2)
    assert sl.status == "working" and sl.qty == 200


def test_replace_and_expire():
    e = MatchingEngine()
    o = order("buy", 100, "limit", limit_price=90)
    e.submit(o, q(99.99, 100.00), 1)
    e.replace(o.id, 2, limit_price=D("100"))
    assert fills(e.on_quote(q(99.99, 100.00, trade=False), 3))
    d = order("buy", 100, "limit", limit_price=90)
    e.submit(d, q(99.99, 100.00), 4)
    e.expire(5, lambda x: x.tif == "day")
    assert d.status == "expired"


# ---- property tests ----------------------------------------------------------
price = st.integers(min_value=100, max_value=20000).map(lambda c: Decimal(c) / 100)
size = st.integers(min_value=0, max_value=1500)


@st.composite
def quote_tape(draw):
    out = []
    for _ in range(draw(st.integers(1, 25))):
        bid = draw(price)
        ask = bid + Decimal(draw(st.integers(1, 30))) / 100
        last = draw(st.sampled_from([bid, ask, (bid + ask) / 2]))
        out.append(q(bid, ask, last, draw(size), draw(size), draw(st.integers(1, 800)), draw(st.booleans())))
    return out


@st.composite
def random_order(draw):
    typ = draw(st.sampled_from(["market", "limit", "stop", "stop_limit", "trailing_stop"]))
    kw = {"tif": draw(st.sampled_from(["day", "gtc", "ioc", "fok"]))}
    if typ in ("limit", "stop_limit"):
        kw["limit_price"] = draw(price)
    if typ in ("stop", "stop_limit"):
        kw["stop_price"] = draw(price)
    if typ == "trailing_stop":
        kw["trail_amount"] = Decimal(draw(st.integers(1, 500))) / 100
    if typ in ("stop", "stop_limit", "trailing_stop"):
        kw["tif"] = draw(st.sampled_from(["day", "gtc"]))
    return order(draw(st.sampled_from(["buy", "sell"])), draw(st.integers(1, 3000)), typ, **kw)


@settings(max_examples=400, deadline=None)
@given(random_order(), quote_tape())
def test_engine_invariants(o, tape):
    e = MatchingEngine()
    was_stop = o.type in ("stop", "stop_limit", "trailing_stop")
    events = e.submit(o, tape[0], 0)
    seen_trigger_print = o.triggered
    all_fills = fills(events)
    for i, quote in enumerate(tape[1:], start=1):
        before = o.filled_qty
        ev = e.on_quote(quote, i)
        new = fills(ev)
        all_fills += new
        if o.status in ("filled", "canceled", "expired"):
            assert not fills(e.on_quote(quote, i))  # terminal orders never trade again
        if o.tif in ("ioc", "fok"):
            assert not new  # immediate orders only trade at submission
    total = sum((f.qty for f in all_fills), Decimal(0))
    assert total == o.filled_qty <= o.qty
    assert all(f.qty > 0 and f.price > 0 for f in all_fills)
    if o.limit_price is not None:
        for f in all_fills:
            assert f.price <= o.limit_price if o.side == "buy" else f.price >= o.limit_price
    if o.tif == "fok":
        assert o.filled_qty in (0, o.qty)
    if o.status == "filled":
        assert o.filled_qty == o.qty
    if was_stop and all_fills:
        assert o.triggered
    assert o.status in ("working", "partially_filled", "filled", "canceled")
    if o.filled_qty:
        notional = sum((f.qty * f.price for f in all_fills), Decimal(0))
        assert abs(notional / o.filled_qty - o.avg_fill_price) <= Decimal("0.0002")


@settings(max_examples=200, deadline=None)
@given(quote_tape(), st.integers(1, 500), price, price)
def test_oco_never_fills_both_sides_beyond_position(tape, qty, tp_px, sl_px):
    e = MatchingEngine()
    parent = order("buy", qty)
    tp = order("sell", qty, "limit", limit_price=tp_px, tif="gtc")
    sl = order("sell", qty, "stop", stop_price=sl_px, tif="gtc")
    e.submit_bracket(parent, [tp, sl], tape[0], 0)
    for i, quote in enumerate(tape[1:], start=1):
        e.on_quote(quote, i)
    assert tp.filled_qty + sl.filled_qty <= parent.filled_qty
