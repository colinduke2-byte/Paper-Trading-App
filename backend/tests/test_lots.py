"""Tax lots: lot ids, lot methods, specific-lot orders, the lot viewer, wash sales and the tax-year summary.

Every expected number is worked out by hand in the comment next to it.
"""
import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

from app.analytics import export, wash
from app.analytics.trades import closed_lots, gains_report, tax_year_summary
from app.api.server import create_app
from app.core import calendar as cal
from app.core.calendar import NY
from app.core.models import Fill
from app.exchange import OrderRejected
from app.ledger.account import LOT_METHODS, Account, long_term_from, pick_lot
from app.ledger.ledger import position_account
from app.lifecycle.actions import split
from app.options import chain
from app.options.symbology import build
from helpers import D, T0, make_exchange, move


def ts(y, m, d, h=15):
    return datetime(y, m, d, h, tzinfo=NY).timestamp()


def fill(i, side, qty, price, symbol="ABC", fee="0", t=None):
    return Fill(id=f"F{i}", order_id=None, account_id="A1", symbol=symbol, side=side, qty=D(str(qty)),
                price=D(str(price)), ts=float(i) if t is None else t, fee=D(fee))


def account(*buys, method="fifo"):
    """Account with one 10-share ABC lot per price in `buys`, bought at times 1, 2, 3, ..."""
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.lot_method = method
    for i, px in enumerate(buys, 1):
        a.apply_fill(fill(i, "buy", 10, px), 1)
    return a


def ledger_ok(a, symbols=("ABC",)):
    assert {k: v for k, v in a.ledger.recompute().items() if v} == {k: v for k, v in a.ledger.balances.items() if v}
    assert sum(a.ledger.balances.values()) == 0
    for s in symbols:
        assert a.ledger.balance(position_account(s)) == a.cost_basis(s)


# ---- lot ids ---------------------------------------------------------------------------------------
def test_lot_ids_are_stable_through_partial_closes_and_storage():
    a = account("100", "120", "90")
    assert [l.id for l in a.lots["ABC"]] == ["L1", "L2", "L3"]
    a.apply_fill(fill(4, "sell", 15, "110"), 1)  # FIFO: all of L1, 5 of L2
    assert [(c["lot_id"], c["qty"]) for c in a.closed] == [("L1", "10"), ("L2", "5")]
    assert [(l.id, l.qty) for l in a.lots["ABC"]] == [("L2", 5), ("L3", 10)]
    b = Account.from_plain(json.loads(json.dumps(a.to_plain())))
    assert [l.id for l in b.lots["ABC"]] == ["L2", "L3"] and b.lot_seq == 4
    b.apply_fill(fill(5, "buy", 1, "100"), 1)
    assert b.lots["ABC"][-1].id == "L4"  # never reuses L1


def test_old_state_files_get_lot_ids_and_keep_working():
    a = account("100", "120")
    a.apply_fill(fill(3, "sell", 5, "110"), 1)
    old = a.to_plain()
    del old["lot_seq"]
    old["lots"] = {s: [l[:5] for l in ls] for s, ls in old["lots"].items()}  # five-field lots: no id
    for c in old["closed"]:
        del c["lot_id"]
    b = Account.from_plain(old)
    assert [l.id for l in b.lots["ABC"]] == ["L1", "L2"] and b.lot_seq == 3
    assert b.cash == a.cash and b.positions() == a.positions()
    rep = gains_report(b)
    assert rep["rows"][0]["lot_id"] == "" and rep["totals"]["gain"] == b.realized
    # the old closed row is matched to the open lot it came from by its opening fill: not its own replacement
    assert not rep["rows"][0]["wash_sale"]
    b.apply_fill(fill(4, "sell", 15, "90"), 1, plan=[{"lot_id": "L2", "qty": "10"}, {"lot_id": "L1", "qty": "5"}])
    assert b.position("ABC") == 0 and [c["lot_id"] for c in b.closed[-2:]] == ["L2", "L1"]


# ---- lot methods: reference cases -------------------------------------------------------------------
@pytest.mark.parametrize("method,gain,closed,basis", [
    # lots 10 @100, 10 @120, 10 @90; sell 15 @110
    # lowest cost: 10 @90 (+200) then 5 of @100 (+50) = +250; left 5 @100 + 10 @120 = 1700
    ("lowest_cost", "250", [("L3", "10"), ("L1", "5")], "1700"),
    # tax-efficient: per-unit gains at 110 are +10, -10, +20: L2 (-100) then 5 of L1 (+50) = -50; left 500 + 900
    ("tax_loss", "-50", [("L2", "10"), ("L1", "5")], "1400"),
    ("highest_cost", "-50", [("L2", "10"), ("L1", "5")], "1400"),
    ("lifo", "150", [("L3", "10"), ("L2", "5")], "1600"),
    ("fifo", "50", [("L1", "10"), ("L2", "5")], "1500"),
])
def test_lot_method_reference_cases(method, gain, closed, basis):
    a = account("100", "120", "90", method=method)
    assert a.apply_fill(fill(4, "sell", 15, "110"), 1) == D(gain)
    assert [(c["lot_id"], c["qty"]) for c in a.closed] == closed
    assert a.cost_basis("ABC") == D(basis) and a.cash == D("98550")
    assert {c["lot_method"] for c in a.closed} == {method}


def test_new_methods_on_a_short_position():
    # short 10 @50 (L1) and 10 @40 (L2), cover 10 @45.
    # lowest cost mirrors to "sold highest first": covers L1, 500 - 450 = +50.
    # tax-efficient: gain per unit is +5 on L1 and -5 on L2: covers L2, 400 - 450 = -50.
    for method, gain, lot in (("lowest_cost", "50", "L1"), ("tax_loss", "-50", "L2")):
        a = Account("A1", "t", "margin", D("100000"), 0)
        a.lot_method = method
        a.apply_fill(fill(1, "sell", 10, "50"), 1)
        a.apply_fill(fill(2, "sell", 10, "40"), 1)
        assert a.apply_fill(fill(3, "buy", 10, "45"), 1) == D(gain)
        assert a.closed[0]["lot_id"] == lot


def test_tax_loss_prefers_long_term_on_equal_loss_and_otherwise_the_bigger_loss():
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.apply_fill(fill(1, "buy", 10, "100", t=ts(2026, 5, 1)), 1)  # L1, short-term on 2026-06-01
    a.apply_fill(fill(2, "buy", 10, "100", t=ts(2025, 1, 2)), 1)  # L2, long-term (listed second)
    a.apply_fill(fill(3, "buy", 10, "99", t=ts(2025, 1, 2)), 1)   # L3, long-term, smaller loss
    lots, when = a.lots["ABC"], ts(2026, 6, 1)
    assert pick_lot(lots, "tax_loss", D("90"), when) == 1  # equal loss of 10: the long-term L2
    assert pick_lot(lots, "highest_cost", D("90"), when) == 0  # highest cost: equal price, oldest in the list
    a.lots["ABC"][2].price = D("101")
    assert pick_lot(lots, "tax_loss", D("90"), when) == 2  # a loss of 11 beats 10 whatever the term


def test_long_term_date():
    assert long_term_from(ts(2025, 1, 15)) == date(2026, 1, 16)
    assert long_term_from(ts(2024, 2, 29)) == date(2025, 3, 1)


def test_unknown_lot_method_in_a_state_file_falls_back_to_fifo():
    d = account("100").to_plain()
    d["lot_method"] = "magic"
    assert Account.from_plain(d).lot_method == "fifo"
    d["lot_method"] = "tax_loss"
    assert Account.from_plain(d).lot_method == "tax_loss"


# ---- specific lots: the account ---------------------------------------------------------------------
def test_specific_lots_over_several_fills_at_different_prices():
    a = account("100", "120", "90")
    plan = [{"lot_id": "L3", "qty": "4"}, {"lot_id": "L1", "qty": "6"}]
    assert a.apply_fill(fill(4, "sell", 3, "110"), 1, plan) == D("60")    # 3 of L3: 3 x 20
    assert a.apply_fill(fill(5, "sell", 5, "111"), 1, plan) == D("65")    # 1 of L3 (+21), 4 of L1 (+44)
    assert a.apply_fill(fill(6, "sell", 2, "112"), 1, plan) == D("24")    # 2 of L1: 2 x 12
    assert [(c["lot_id"], c["qty"], c["close_price"]) for c in a.closed] == [
        ("L3", "3", "110"), ("L3", "1", "111"), ("L1", "4", "111"), ("L1", "2", "112")]
    assert {c["lot_method"] for c in a.closed} == {"specific"}
    assert [e["filled"] for e in plan] == ["4", "6"]
    assert [(l.id, l.qty) for l in a.lots["ABC"]] == [("L1", 4), ("L2", 10), ("L3", 6)]
    ledger_ok(a)


def test_a_named_lot_that_shrank_is_completed_by_the_method():
    a = account("100", "120", "90")
    a.apply_fill(fill(4, "sell", 8, "95"), 1, [{"lot_id": "L3", "qty": "8"}])  # L3 down to 2
    plan = [{"lot_id": "L3", "qty": "5"}]
    a.apply_fill(fill(5, "sell", 5, "100"), 1, plan)
    # 2 left of L3 (specific), the other 3 by FIFO from L1, marked as a fallback
    assert [(c["lot_id"], c["qty"], c["lot_method"], c.get("lot_fallback", False)) for c in a.closed[1:]] == [
        ("L3", "2", "specific", False), ("L1", "3", "fifo", True)]
    assert plan[0]["filled"] == "2"


def test_the_method_skips_lots_other_orders_have_named():
    a = account("100", "120", "90")
    a.apply_fill(fill(4, "sell", 12, "100"), 1, reserved={"L1": D("10"), "L2": D("4")})
    # FIFO without L1 (all named) and the named 4 of L2: 6 of L2, then 6 of L3
    assert [(c["lot_id"], c["qty"]) for c in a.closed] == [("L2", "6"), ("L3", "6")]
    # when only named quantity is left, it is used rather than refusing the fill
    a.apply_fill(fill(5, "sell", 16, "100"), 1, reserved={"L1": D("10"), "L2": D("4")})
    assert [(c["lot_id"], c["qty"]) for c in a.closed[2:]] == [("L3", "4"), ("L1", "10"), ("L2", "2")]
    assert a.position("ABC") == 2


# ---- specific lots: orders ------------------------------------------------------------------------------
def exchange_with_lots(*prices, type="margin"):
    ex, feed, clock = make_exchange(None, ABC="100.00", XYZ="50.00")
    a = ex.create_account("t", type, D("100000")).id
    for px in prices:
        move(ex, feed, clock, "ABC", px)
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    return ex, feed, clock, a


def sell(qty, *lots, **kw):
    req = {"symbol": "ABC", "side": "sell", "qty": qty, **kw}
    if lots:
        req["lots"] = [{"lot_id": l, "qty": q} for l, q in lots]
    return req


def test_lot_choice_is_validated_at_entry():
    ex, feed, clock, a = exchange_with_lots("100", "120", "90")
    ex.submit(a, {"symbol": "XYZ", "side": "buy", "qty": 5})  # L4 is an XYZ lot
    cases = [
        (sell(5, ("L9", 5)), "lot L9 is not an open lot of ABC"),
        (sell(5, ("L4", 5)), r"lot L4 is not an open lot of ABC \(it belongs to XYZ\)"),
        ({"symbol": "ABC", "side": "buy", "qty": 5, "lots": [{"lot_id": "L1", "qty": 5}]}, "lot L1 is a long lot; sell to close it"),
        (sell(8, ("L1", 5), ("L2", 2)), "the chosen lots add up to 7, but the order is for 8"),
        (sell(11, ("L1", 11)), "lot L1 has 10 open; 11 is more than the 10 available"),
        (sell(10, ("L1", 5), ("L1", 5)), "lot L1 is named twice"),
        (sell(5, ("L1", 0), ("L2", 5)), "quantity must be positive"),
        (sell(5, ("L1", "x")), "quantity is not a number"),
        ({**sell(5, ("L1", 5)), "take_profit": "200"}, "not on bracket orders"),
        ({"symbol": "ABC", "side": "sell", "qty": 5, "lots": "L1"}, "lots must be a list"),
    ]
    for req, msg in cases:
        with pytest.raises(OrderRejected, match=msg):
            ex.submit(a, req)
        with pytest.raises(OrderRejected, match=msg):
            ex.preview(a, req)
    with pytest.raises(OrderRejected, match="not on one-cancels-other orders"):
        ex.submit(a, {"oco": [sell(5, ("L1", 5), type="limit", limit_price="200", tif="gtc"),
                              sell(5, type="stop", stop_price="50", tif="gtc")]})
    with pytest.raises(OrderRejected, match="not on multi-leg orders"):
        ex.submit(a, {"legs": [{"symbol": "ABC", "side": "sell", "ratio": 100}, {"symbol": "XYZ", "side": "buy", "ratio": 100}],
                      "qty": 1, "lots": [{"lot_id": "L1", "qty": 100}]})
    # a lot another open order has named cannot be named again beyond what is left of it
    ex.submit(a, sell(6, ("L1", 6), type="limit", limit_price="200", tif="gtc"))
    with pytest.raises(OrderRejected, match="of which 6 is already named by another open order; 5 is more than the 4 available"):
        ex.submit(a, sell(5, ("L1", 5)))
    assert ex.submit(a, sell(4, ("L1", 4), type="limit", limit_price="200", tif="gtc"))[0].status == "working"
    assert ex.reserved_lots(a) == {"L1": D("10")}
    assert ex.account(a).closed == []  # nothing rejected above traded


def test_working_order_closes_its_lots_over_partial_fills_and_other_sales_skip_them():
    ex, feed, clock, a = exchange_with_lots("100", "120", "90")
    pre = ex.preview(a, sell(15, ("L2", 10), ("L3", 5)))["lots"]
    # at the 89.99 bid: L2 900 - 1200 = -300 (short-term), L3 5 of 900 cost 450 -> 449.95 - 450 = -0.05
    assert [(x["lot_id"], x["qty"], x["estimated_gain"], x["method"]) for x in pre] == \
        [("L2", 10, D("-300.10"), "specific"), ("L3", 5, D("-0.05"), "specific")]
    [o] = ex.submit(a, sell(15, ("L2", 10), ("L3", 5), type="limit", limit_price="105", tif="gtc"))
    # a market sale without lots takes the lot nobody named
    ex.submit(a, sell(4))
    assert [(c["lot_id"], c["qty"]) for c in ex.account(a).closed] == [("L1", "4")]
    for px, size in (("105.50", 4), ("106.25", 6), ("107.00", 100)):
        move(ex, feed, clock, "ABC", px, bid_size=size)
    assert o.status == "filled"
    rows = [c for c in ex.account(a).closed if c["order_id"] == o.id]
    per_lot = {}
    for c in rows:
        per_lot[c["lot_id"]] = per_lot.get(c["lot_id"], D(0)) + D(c["qty"])
    assert per_lot == {"L2": D(10), "L3": D(5)} and {c["lot_method"] for c in rows} == {"specific"}
    assert len({c["close_price"] for c in rows}) > 1  # filled at several prices
    assert [(l.id, l.qty) for l in ex.account(a).lots["ABC"]] == [("L1", 6), ("L3", 5)]
    assert [e["filled"] for e in o.lots] == ["10", "5"]
    ledger_ok(ex.account(a))


def test_an_order_whose_named_lot_is_closed_by_another_trade_is_canceled():
    ex, feed, clock, a = exchange_with_lots("100", "120")
    [o] = ex.submit(a, sell(10, ("L1", 10), type="limit", limit_price="200", tif="gtc"))
    ex.submit(a, sell(15))  # 10 from L2, then 5 of the named L1: nothing else is left
    assert o.status == "canceled" and o.reason == "Canceled: lot L1 named by this order was closed by another trade"
    assert ex.reserved_lots(a) == {}
    move(ex, feed, clock, "ABC", "250")
    assert ex.account(a).position("ABC") == 5  # the canceled order never sold the rest, and never went short


def test_quantity_of_a_lot_order_cannot_be_changed_but_its_price_can():
    ex, feed, clock, a = exchange_with_lots("100", "120")
    [o] = ex.submit(a, sell(10, ("L2", 10), type="limit", limit_price="200", tif="gtc"))
    with pytest.raises(OrderRejected, match="cancel it and enter a new one"):
        ex.replace(a, o.id, {"qty": 5})
    assert ex.replace(a, o.id, {"limit_price": "150"}).limit_price == D("150")
    ex.cancel(a, o.id)
    assert ex.reserved_lots(a) == {}


def test_short_lots_cover_in_the_chosen_order():
    ex, feed, clock, a = exchange_with_lots()
    for px in ("50", "40"):
        move(ex, feed, clock, "ABC", px)
        ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 10})
    acc = ex.account(a)
    assert [(l.id, l.qty) for l in acc.lots["ABC"]] == [("L1", -10), ("L2", -10)]
    with pytest.raises(OrderRejected, match="lot L1 is a short lot; buy to close it"):
        ex.submit(a, sell(5, ("L1", 5)))
    move(ex, feed, clock, "ABC", "45")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "lots": [{"lot_id": "L1", "qty": 10}]})
    # opened at the 49.99 bid, covered at the 45.00 ask: 499.90 - 450.00
    assert [(c["lot_id"], c["qty"], c["pnl"]) for c in acc.closed] == [("L1", "-10", "49.90")]
    ledger_ok(acc)


def _option(kind, strike):
    exp = chain.expirations(cal.ny_date(T0))[0]
    return build("ABC", exp, kind, D(strike)), cal.close_ts(exp)


def test_option_lots_and_assignment_respect_named_lots():
    ex, feed, clock, a = exchange_with_lots()
    call, expiry_ts = _option("C", "100")
    for px in ("100", "110"):
        move(ex, feed, clock, "ABC", px, bid_size=100000, ask_size=100000)
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})  # L1 at 100, L2 at 110
    for px in ("100", "104"):
        move(ex, feed, clock, "ABC", px)
        ex.submit(a, {"symbol": call, "side": "buy", "qty": 1})  # L3 and L4: option lots at two premiums
    acc = ex.account(a)
    assert [l.id for l in acc.lots[call]] == ["L3", "L4"] and acc.lots[call][0].price < acc.lots[call][1].price
    ex.submit(a, {"symbol": call, "side": "sell", "qty": 1, "lots": [{"lot_id": "L4", "qty": 1}]})
    assert acc.closed[-1]["lot_id"] == "L4" and [l.id for l in acc.lots[call]] == ["L3"]
    # sell the remaining call and write one: assignment sells 100 shares. L1 is named by a working order,
    # so the assignment (FIFO) delivers L2 instead and the order stays working.
    ex.submit(a, {"symbol": call, "side": "sell", "qty": 2})
    [o] = ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 100, "type": "limit", "limit_price": "500", "tif": "gtc",
                        "lots": [{"lot_id": "L1", "qty": 100}]})
    clock.set(expiry_ts - 5)
    move(ex, feed, clock, "ABC", "120", seconds=10)
    assigned = [c for c in acc.closed if c["kind"] == "assignment" and c["symbol"] == "ABC"]
    assert [(c["lot_id"], c["qty"]) for c in assigned] == [("L2", "100")]
    assert o.status == "working" and [l.id for l in acc.lots["ABC"]] == ["L1"]
    ledger_ok(acc, ("ABC", call))


def test_split_cancels_lot_orders_and_keeps_lot_ids():
    ex, feed, clock, a = exchange_with_lots("100", "120")
    [o] = ex.submit(a, sell(10, ("L2", 10), type="limit", limit_price="500", tif="gtc"))
    feed.schedule(split("ABC", date(2026, 11, 25), 4))
    clock.advance(86400)
    ex.tick()
    acc = ex.account(a)
    assert o.status == "canceled" and "stock split" in o.reason and ex.reserved_lots(a) == {}
    assert [(l.id, l.qty, l.price) for l in acc.lots["ABC"]] == [("L1", 40, D("25")), ("L2", 40, D("30"))]
    ex.submit(a, sell(40, ("L2", 40)))
    assert acc.closed[-1]["lot_id"] == "L2" and acc.position("ABC") == 40


def test_option_lots_keep_their_ids_through_a_split_adjustment():
    ex, feed, clock, a = exchange_with_lots()
    exp = chain.expirations(date(2026, 11, 23))[-1]
    call = build("ABC", exp, "C", D("105"))
    ex.submit(a, {"symbol": call, "side": "buy", "qty": 2})
    feed.schedule(split("ABC", date(2026, 11, 25), 4))
    clock.advance(86400)
    ex.tick()
    acc = ex.account(a)
    [new] = list(acc.lots)
    assert new == build("ABC", exp, "C", D("26.25")) and [(l.id, l.qty) for l in acc.lots[new]] == [("L1", 8)]


def test_lot_orders_survive_a_restart(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("t", "margin", D("100000")).id
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    ex.submit(a, sell(4, ("L1", 4), type="limit", limit_price="500", tif="gtc"))
    ex.save(force=True)
    ex2, _, _ = make_exchange(str(tmp_path))
    ex2.load()
    assert ex2.reserved_lots(a) == {"L1": D("4")}
    with pytest.raises(OrderRejected, match="already named"):
        ex2.submit(a, sell(7, ("L1", 7)))


# ---- the lot viewer and reports API --------------------------------------------------------------------
def test_lots_api_and_tax_summary_api():
    ex, feed, clock, a = exchange_with_lots("100", "90")
    c = TestClient(create_app(ex, run_loop=False))
    c.post(f"/api/accounts/{a}/orders", json=sell(3, ("L2", 3), type="limit", limit_price="500", tif="gtc"))
    move(ex, feed, clock, "ABC", "95")
    d = c.get(f"/api/accounts/{a}/lots").json()
    [p] = d["positions"]
    assert p["symbol"] == "ABC" and p["mark"] == 95 and p["source"] == ex.quote_source("ABC")
    l1, l2 = p["lots"]
    # L1: 10 bought at the 100.00 ask; at 95: value 950, unrealised -50
    assert (l1["id"], l1["qty"], l1["cost_per_unit"], l1["cost_basis"], l1["market_value"], l1["unrealized_pnl"]) == \
        ("L1", 10, 100, 1000, 950, -50)
    assert l1["term"] == "short" and l1["long_term_on"] == "2027-11-25" and l1["opened_by"] == "trade"
    assert (l2["reserved"], l2["available"]) == (3, 7) and l1["wash_sale"] is None
    assert c.get(f"/api/accounts/{a}/lots?symbol=XYZ").json()["positions"] == []
    r = c.post(f"/api/accounts/{a}/orders", json=sell(8, ("L2", 8)))
    assert r.status_code == 422 and "already named by another open order" in r.json()["detail"]
    assert c.post(f"/api/accounts/{a}/orders", json=sell(5, ("L1", 5))).status_code == 200
    t = c.get(f"/api/accounts/{a}/tax-summary").json()
    # 5 of L1 sold at the 94.99 bid: 474.95 - 500 = -25.05
    assert t["year"] == 2026 and t["years"] == [2026] and t["short_term"]["gain"] == -25.05 and t["long_term"]["lots"] == 0
    assert c.get(f"/api/accounts/{a}/tax-summary?year=2025").json()["total"]["lots"] == 0
    assert c.get(f"/api/accounts/{a}/tax-summary?year=0").status_code in (400, 422)
    opts = c.get(f"/api/accounts/{a}/lot-method").json()["options"]
    assert set(opts) == set(LOT_METHODS)
    assert c.put(f"/api/accounts/{a}/lot-method", json={"method": "tax_loss"}).json()["method"] == "tax_loss"
    g = list(csv.DictReader(io.StringIO(c.get(f"/api/accounts/{a}/export/gains.csv").text)))
    # L2 was bought the same day and is still held, so the 25.05 loss is a wash sale replaced by 5 of L2
    assert g[0]["tax_lot_id"] == "L1" and g[0]["lot_method"] == "specific" and g[0]["wash_sale_disallowed"] == "25.05"
    assert g[0]["wash_sale_replacements"] == "L2:5:25.05" and g[0]["adjusted_gain"] == "0.00"
    assert t["short_term"]["wash_sale_disallowed"] == 25.05 and t["adjusted"]["total"] == 0


# ---- wash sales: reference cases ----------------------------------------------------------------------
def test_wash_sale_basis_and_holding_period_carry_into_the_replacement_lot():
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.apply_fill(fill(1, "buy", 10, "100", t=ts(2026, 3, 1)), 1)    # L1
    a.apply_fill(fill(2, "sell", 10, "90", t=ts(2026, 3, 11)), 1)   # loss 100, held 10 days
    a.apply_fill(fill(3, "buy", 10, "91", t=ts(2026, 3, 16)), 1)    # L2, the replacement
    r = gains_report(a)["rows"][0]
    assert r["wash_sale"] and r["wash_sale_disallowed"] == D("100") and r["adjusted_gain"] == 0
    [x] = r["wash_sale_with"]
    # replacement basis 910 + 100 = 1010; holding period starts 10 days before 16 March
    assert (x["lot_id"], x["qty"], x["basis_before"], x["basis_after"], x["adjusted_open_date"], x["status"]) == \
        ("L2", D(10), D("910"), D("1010"), "2026-03-06", "open")
    # the open lot carries the adjustment
    ex, feed, clock = make_exchange()
    from app.api.routes_lots import lot_rows
    [p] = lot_rows(ex, a)
    w = p["lots"][0]["wash_sale"]
    assert w["disallowed_loss_added"] == D("100") and w["adjusted_cost_basis"] == D("1010")
    assert w["adjusted_holding_start"] == "2026-03-06" and w["from_closed_lots"] == ["C1"]
    # selling the replacement at 95: ledger gain 950 - 910 = +40, adjusted 950 - 1010 = -60 (nothing replaces it)
    a.apply_fill(fill(4, "sell", 10, "95", t=ts(2026, 4, 20)), 1)
    r2 = gains_report(a)["rows"][1]
    assert (r2["gain"], r2["basis_adjustment"], r2["adjusted_cost"], r2["adjusted_gain"], r2["wash_sale"]) == \
        (D("40"), D("100"), D("1010"), D("-60"), False)
    assert r2["adjusted_open_date"] == "2026-03-06"
    t = gains_report(a)["totals"]
    assert t["gain"] == D("-60") == t["adjusted_gain"] and t["wash_sale_disallowed"] == D("100")


def test_wash_sale_partial_replacement_is_pro_rata():
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.apply_fill(fill(1, "buy", 10, "100", t=ts(2026, 3, 1)), 1)
    a.apply_fill(fill(2, "sell", 10, "90", t=ts(2026, 3, 11)), 1)  # loss 100
    a.apply_fill(fill(3, "buy", 4, "91", t=ts(2026, 3, 20)), 1)    # 4 replacement shares: 4/10 of the loss
    r = gains_report(a)["rows"][0]
    assert r["wash_sale_disallowed"] == D("40") and r["adjusted_gain"] == D("-60")


def test_each_replacement_share_is_used_once_and_shares_sold_together_do_not_replace_each_other():
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.apply_fill(fill(1, "buy", 10, "100", t=ts(2026, 3, 1)), 1)   # L1
    a.apply_fill(fill(2, "buy", 10, "100", t=ts(2026, 3, 2)), 1)   # L2
    a.apply_fill(fill(3, "sell", 20, "90", t=ts(2026, 3, 10)), 1)  # one sale: L1 -100, L2 -100
    a.apply_fill(fill(4, "buy", 10, "91", t=ts(2026, 3, 15)), 1)   # L3 replaces the first loss only
    rows = gains_report(a)["rows"]
    assert [(r["lot_id"], r["wash_sale_disallowed"]) for r in rows] == [("L1", D("100")), ("L2", D("0"))]
    assert rows[0]["wash_sale_with"][0]["lot_id"] == "L3"


def test_wash_sale_on_a_specific_lot_sale_finds_the_lot_still_held():
    # L1 bought 1 Mar at 100, L2 bought 20 Mar at 95; sell L1 by name on 25 Mar at 90: L2 (held) replaces it
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.lot_method = "lifo"  # the method would have sold L2; the order names L1
    a.apply_fill(fill(1, "buy", 10, "100", t=ts(2026, 3, 1)), 1)
    a.apply_fill(fill(2, "buy", 10, "95", t=ts(2026, 3, 20)), 1)
    a.apply_fill(fill(3, "sell", 10, "90", t=ts(2026, 3, 25)), 1, [{"lot_id": "L1", "qty": "10"}])
    r = gains_report(a)["rows"][0]
    assert r["lot_method"] == "specific" and r["wash_sale_with"][0]["lot_id"] == "L2"
    # L1 held 24 days: L2's holding period starts 24 days before 20 March
    assert r["wash_sale_with"][0]["adjusted_open_date"] == "2026-02-24"


def test_short_sale_wash():
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.apply_fill(fill(1, "sell", 10, "50", t=ts(2026, 3, 1)), 1)   # L1 short
    a.apply_fill(fill(2, "buy", 10, "55", t=ts(2026, 3, 5)), 1)    # cover: loss 50
    a.apply_fill(fill(3, "sell", 10, "52", t=ts(2026, 3, 10)), 1)  # L2: another short sale replaces it
    a.apply_fill(fill(4, "buy", 10, "52", t=ts(2026, 3, 20)), 1)   # cover L2 flat: gain 0, adjusted -50
    rows = gains_report(a)["rows"]
    assert rows[0]["wash_sale_disallowed"] == D("50") and rows[0]["wash_sale_with"][0]["lot_id"] == "L2"
    assert rows[1]["gain"] == 0 and rows[1]["adjusted_gain"] == D("-50") and rows[1]["adjusted_term"] == "short"


def test_tax_year_summary_reference():
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.apply_fill(fill(1, "buy", 10, "100", t=ts(2024, 6, 3)), 1)   # L1
    a.apply_fill(fill(2, "buy", 10, "100", t=ts(2025, 12, 1)), 1)  # L2
    a.apply_fill(fill(3, "sell", 10, "130", t=ts(2025, 12, 10)), 1)  # L1 long-term: +300 (2025)
    a.apply_fill(fill(4, "sell", 10, "80", t=ts(2026, 1, 5)), 1)   # L2 short-term: -200 (2026)
    a.apply_fill(fill(5, "buy", 5, "82", t=ts(2026, 1, 20)), 1)    # L3: 5 replacement shares, 100 disallowed
    y25, y26 = tax_year_summary(a, 2025), tax_year_summary(a, 2026)
    assert y25["long_term"]["gain"] == D("300") and y25["short_term"]["lots"] == 0
    assert (y26["short_term"]["gain"], y26["short_term"]["wash_sale_disallowed"], y26["adjusted"]["short_term"]) == \
        (D("-200"), D("100"), D("-100"))
    assert y26["wash_sales"][0]["replacements"][0]["lot_id"] == "L3"
    assert any("not tax advice" in n for n in y26["notes"])


# ---- properties ------------------------------------------------------------------------------------------
@st.composite
def lot_script(draw):
    """Random fills on two symbols; closing fills sometimes name lots and other orders reserve some."""
    steps = []
    for _ in range(draw(st.integers(1, 30))):
        steps.append((draw(st.sampled_from(["buy", "sell"])), Decimal(draw(st.integers(1, 400))) / 10,
                      Decimal(draw(st.integers(100, 500000))) / 100, draw(st.sampled_from(["ABC", "XYZ"])),
                      draw(st.sampled_from(LOT_METHODS)), draw(st.integers(0, 2 ** 16)),
                      draw(st.integers(0, 40)) * 86400))
    return steps


def _random_plan(a, sym, side, qty, seed):
    lots = [l for l in a.lots.get(sym, []) if (l.qty > 0) == (side == "sell")]
    if not lots or seed % 3 == 0:
        return None, None
    plan, left = [], qty
    for i, l in enumerate(lots):
        if (seed >> i) & 1 and left > 0:
            q = min(left, abs(l.qty))
            plan.append({"lot_id": l.id, "qty": str(q)})
            left -= q
    reserved = {l.id: abs(l.qty) / 2 for i, l in enumerate(lots) if (seed >> (i + 8)) & 1}
    return plan or None, reserved


@settings(max_examples=250, deadline=None)
@given(lot_script())
def test_ledger_invariants_and_lot_conservation_for_any_lot_choices(steps):
    a = Account("A1", "t", "margin", D("1000000"), 0)
    opened, t = {}, 0.0
    for i, (side, qty, price, sym, method, seed, gap) in enumerate(steps, 1):
        t += gap + 1
        a.lot_method = method
        plan, reserved = _random_plan(a, sym, side, qty, seed)
        before = {l.id: abs(l.qty) for ls in a.lots.values() for l in ls}
        a.apply_fill(fill(i, side, qty, price, sym, t=t), 1, plan, reserved)
        for l in (l for ls in a.lots.values() for l in ls):
            opened.setdefault(l.id, abs(l.qty))
        # a named lot is never closed beyond what the plan named or what it held
        for e in plan or ():
            assert D(e.get("filled", "0")) <= min(D(e["qty"]), before[e["lot_id"]])
    ledger_ok(a, ("ABC", "XYZ"))
    # every lot's closed quantity plus what is open equals what it opened with: nothing closed twice
    closed = {}
    for c in a.closed:
        closed[c["lot_id"]] = closed.get(c["lot_id"], D(0)) + abs(D(c["qty"]))
    still = {l.id: abs(l.qty) for ls in a.lots.values() for l in ls}
    for lid, q in opened.items():
        assert closed.get(lid, D(0)) + still.get(lid, D(0)) == q
    rep = gains_report(a)
    assert rep["totals"]["gain"] == a.realized == sum((r["gain"] for r in rep["rows"]), D(0))
    assert all(r["proceeds"] - r["cost"] == r["gain"] and r["wash_sale_disallowed"] >= 0 for r in rep["rows"])
    # wash-sale amounts only move loss between lots: once everything is closed, nothing is left over
    if not a.lots:
        assert rep["totals"]["adjusted_gain"] == rep["totals"]["gain"]
    # every disallowed cent is either carried into a later sale or still carried by an open lot,
    # and an open lot never carries more shares than it holds
    _, open_adj = wash.analyse(a, closed_lots(a))
    held = {l.id: abs(l.qty) for ls in a.lots.values() for l in ls}
    assert all(k in held and v["qty"] <= held[k] for k, v in open_adj.items())
    assert rep["totals"]["wash_sale_disallowed"] == rep["totals"]["basis_adjustments"] + \
        sum((v["amount"] for v in open_adj.values()), D(0))
    b = Account.from_plain(json.loads(json.dumps(a.to_plain())))
    assert b.cash == a.cash and gains_report(b)["totals"] == rep["totals"]
    assert [l.id for ls in b.lots.values() for l in ls] == [l.id for ls in a.lots.values() for l in ls]


@st.composite
def order_script(draw):
    steps = []
    for _ in range(draw(st.integers(1, 14))):
        kind = draw(st.integers(0, 3))
        if kind == 0:
            steps.append(("move", Decimal(draw(st.integers(9000, 11000))) / 100, draw(st.integers(1, 30))))
        elif kind == 1:
            steps.append(("buy", draw(st.integers(1, 20))))
        else:
            steps.append(("sell", draw(st.integers(1, 25)), draw(st.integers(0, 2 ** 12)),
                          draw(st.sampled_from(["market", "limit"])), Decimal(draw(st.integers(-200, 200))) / 100))
    return steps


@settings(max_examples=60, deadline=None)
@given(order_script(), st.sampled_from(LOT_METHODS))
def test_reserved_lots_are_never_double_closed(steps, method):
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "margin", D("1000000")).id
    acc = ex.account(a)
    acc.lot_method = method
    opened = {}
    for s in steps:
        if s[0] == "move":
            move(ex, feed, clock, "ABC", s[1], bid_size=s[2], ask_size=1000)
        elif s[0] == "buy":
            ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": s[1]})
        else:
            _, qty, seed, typ, off = s
            req = {"symbol": "ABC", "side": "sell", "qty": qty, "type": typ, "tif": "gtc" if typ == "limit" else "day"}
            if typ == "limit":
                req["limit_price"] = str(ex.quote("ABC").bid + off)
            lots = [l for l in acc.lots.get("ABC", []) if l.qty > 0]
            if seed % 2 and lots:
                pick, left = [], D(qty)
                for i, l in enumerate(lots):
                    if (seed >> (i + 1)) & 1 and left > 0:
                        q = min(left, abs(l.qty))
                        pick.append({"lot_id": l.id, "qty": str(q)})
                        left -= q
                if pick:
                    req["lots"] = pick
                    req["qty"] = str(sum(D(p["qty"]) for p in pick))
            try:
                ex.submit(a, req)
            except OrderRejected:
                pass
        for l in acc.lots.get("ABC", []):
            opened.setdefault(l.id, abs(l.qty))
        # what open orders name never exceeds what the lots hold
        held = {l.id: abs(l.qty) for l in acc.lots.get("ABC", [])}
        for lid, q in ex.reserved_lots(a).items():
            assert q <= held.get(lid, D(0))
    for o in ex.engine.orders.values():
        for e in o.lots or ():
            got = sum((abs(D(c["qty"])) for c in acc.closed if c["order_id"] == o.id and c["lot_id"] == e["lot_id"]
                       and c["lot_method"] == "specific"), D(0))
            assert got == D(e["filled"]) <= D(e["qty"])
            # an order that named lots closed only those lots
            assert all(c["lot_id"] in {x["lot_id"] for x in o.lots} for c in acc.closed if c["order_id"] == o.id)
    closed = {}
    for c in acc.closed:
        closed[c["lot_id"]] = closed.get(c["lot_id"], D(0)) + abs(D(c["qty"]))
    for lid, q in closed.items():
        assert q <= opened[lid]
    ledger_ok(acc)
    assert gains_report(acc)["totals"]["gain"] == acc.realized


# ---- review fixes: regression tests ------------------------------------------------------------------
def test_reservations_of_several_orders_on_one_lot_are_checked_together():
    # Buy 10 (L1); two sell-5 orders each name L1:5; a plain sell of 1 must close part of L1.
    # Only 9 is left for 10 named: the newer order is canceled, the older one keeps its 5.
    ex, feed, clock, a = exchange_with_lots("100")
    [o1] = ex.submit(a, sell(5, ("L1", 5), type="limit", limit_price="150", tif="gtc"))
    clock.advance(1)
    [o2] = ex.submit(a, sell(5, ("L1", 5), type="limit", limit_price="150", tif="gtc"))
    ex.submit(a, sell(1))
    assert o1.status == "working" and o2.status == "canceled"
    assert o2.reason == "Canceled: lot L1 named by this order was closed by another trade"
    assert ex.reserved_lots(a) == {"L1": D("5")}
    move(ex, feed, clock, "ABC", "151", bid_size=1000)
    acc = ex.account(a)
    assert o1.status == "filled" and acc.position("ABC") == 4 and [l.qty for l in acc.lots["ABC"]] == [4]
    assert all(c["lot_method"] == "specific" for c in acc.closed if c["order_id"] == o1.id)
    ledger_ok(acc)


def test_in_one_step_lot_orders_book_first_and_never_go_short():
    # Buy 8 (L1). A plain sell of 6 is entered first, then a sell of 8 naming L1:8; both fill on one quote.
    # The lot order books first and closes exactly L1; the plain order's 6 is the short sale it asked for.
    ex, feed, clock, a = exchange_with_lots()
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 8})
    [plain] = ex.submit(a, sell(6, type="limit", limit_price="150", tif="gtc"))
    [lot] = ex.submit(a, sell(8, ("L1", 8), type="limit", limit_price="150", tif="gtc"))
    move(ex, feed, clock, "ABC", "151", bid_size=1000)
    acc = ex.account(a)
    assert [(c["lot_id"], c["qty"], c["lot_method"]) for c in acc.closed if c["order_id"] == lot.id] == [("L1", "8", "specific")]
    assert lot.status == "filled" and plain.status == "filled"
    [short] = acc.lots["ABC"]
    assert short.qty == -6 and short.fill_id in {f.id for f in acc.fills if f.order_id == plain.id}
    ledger_ok(acc)


def test_a_lot_order_fill_is_clipped_to_what_its_lots_hold():
    # If a named lot is smaller at booking time than the order still needs (here shrunk behind the
    # exchange's back), the fill is cut to what the lot holds and the rest of the order is canceled.
    ex, feed, clock, a = exchange_with_lots("100")
    [o] = ex.submit(a, sell(10, ("L1", 10), type="limit", limit_price="150", tif="gtc"))
    acc = ex.account(a)
    acc.apply_fill(fill(99, "sell", 3, "100", t=ex.now()), 1)  # L1 down to 7
    move(ex, feed, clock, "ABC", "151", bid_size=1000)
    rows = [c for c in acc.closed if c["order_id"] == o.id]
    assert [(c["lot_id"], c["qty"]) for c in rows] == [("L1", "7")]
    assert o.status == "canceled" and o.filled_qty == 7 and o.avg_fill_price == D("150.99")
    assert o.reason == "Canceled: lot L1 named by this order was closed by another trade"
    assert acc.position("ABC") == 0 and "ABC" not in acc.lots
    assert sum(f.qty for f in acc.fills if f.order_id == o.id) == 7
    ledger_ok(acc)


@st.composite
def one_step_script(draw):
    buys = draw(st.lists(st.integers(1, 10), min_size=1, max_size=4))
    orders = []
    for _ in range(draw(st.integers(1, 6))):
        orders.append((draw(st.integers(0, 2)), draw(st.integers(1, 12)), draw(st.integers(0, 2 ** 8))))
    return buys, orders, draw(st.integers(1, 40)), draw(st.booleans())


@settings(max_examples=150, deadline=None)
@given(one_step_script(), st.sampled_from(LOT_METHODS))
def test_lot_orders_filling_in_one_step_never_short_and_never_close_unnamed_lots(script, method):
    buys, orders, size, shrink = script
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "margin", D("1000000")).id
    acc = ex.account(a)
    acc.lot_method = method
    for q in buys:
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": q})
    lot_orders = []
    for kind, qty, seed in orders:
        req = {"symbol": "ABC", "side": "sell", "qty": qty, "type": "limit", "limit_price": "150", "tif": "gtc"}
        if kind:  # name lots
            pick, left = [], D(qty)
            for i, l in enumerate(acc.lots.get("ABC", [])):
                free = abs(l.qty) - ex.reserved_lots(a).get(l.id, D(0))
                if l.qty > 0 and (seed >> i) & 1 and left > 0 and free > 0:
                    q = min(left, free)
                    pick.append({"lot_id": l.id, "qty": str(q)})
                    left -= q
            if not pick:
                continue
            req.update(lots=pick, qty=str(sum(D(p["qty"]) for p in pick)))
        try:
            [o] = ex.submit(a, req)
        except OrderRejected:
            continue
        if kind:
            lot_orders.append(o)
    if shrink:
        ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 1})
    move(ex, feed, clock, "ABC", "151", bid_size=size, ask_size=1000)
    for o in lot_orders:
        named = {e["lot_id"] for e in o.lots}
        rows = [c for c in acc.closed if c["order_id"] == o.id]
        assert all(c["lot_id"] in named and c["lot_method"] == "specific" for c in rows)
        filled = sum((f.qty for f in acc.fills if f.order_id == o.id), D(0))
        assert filled == sum((abs(D(c["qty"])) for c in rows), D(0)) == o.filled_qty  # never opened a lot
        fill_ids = {f.id for f in acc.fills if f.order_id == o.id}
        assert not any(l.fill_id in fill_ids for l in acc.lots.get("ABC", []))
    ledger_ok(acc)


def test_a_replacement_share_already_carrying_a_loss_is_not_used_again():
    # d0 buy 10 @100 (L1); d1 buy 5 @100 (L2); d2 sell 1 of L2 @105 (named); d3 sell 4 of L1 @90: a loss of 40,
    # replaced by the 4 shares left in L2. d4 sell 1 of L1 @90: L2's 4 shares already carry the first loss,
    # so nothing replaces this one: 0 disallowed, and L2 carries 4 shares / 40.00.
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.apply_fill(fill(1, "buy", 10, "100", t=ts(2026, 3, 2)), 1)
    a.apply_fill(fill(2, "buy", 5, "100", t=ts(2026, 3, 3)), 1)
    a.apply_fill(fill(3, "sell", 1, "105", t=ts(2026, 3, 4)), 1, [{"lot_id": "L2", "qty": "1"}])
    a.apply_fill(fill(4, "sell", 4, "90", t=ts(2026, 3, 5)), 1)
    a.apply_fill(fill(5, "sell", 1, "90", t=ts(2026, 3, 6)), 1)
    rows = gains_report(a)["rows"]
    assert [(r["lot_id"], r["wash_sale_disallowed"]) for r in rows] == [("L2", D(0)), ("L1", D("40")), ("L1", D(0))]
    _, open_adj = wash.analyse(a, closed_lots(a))
    assert (open_adj["L2"]["qty"], open_adj["L2"]["amount"]) == (D(4), D("40"))


def test_wash_sale_analysis_scales_and_is_cached():
    # 10,000 round trips (20,000 fills) in one trading day, every other one at a loss with the next
    # purchase as its replacement. Measured about 0.5 s here; the bound leaves room for slow machines
    # (it was 30 s at 6,000 round trips before the windowed index).
    import time
    a = Account("A1", "t", "margin", D("100000000"), 0)
    t0 = ts(2026, 1, 2)
    for i in range(10000):
        a.apply_fill(fill(2 * i, "buy", 10, "100", t=t0 + 2 * i), 1)
        a.apply_fill(fill(2 * i + 1, "sell", 10, "99" if i % 2 else "101", t=t0 + 2 * i + 1), 1)
    start = time.perf_counter()
    rep = gains_report(a)
    assert time.perf_counter() - start < 3.0
    # each 10-share loss of 10.00 is replaced by the next round trip's purchase; the last one has none
    assert rep["totals"]["wash_sale_disallowed"] == D("49990")
    assert rep["totals"]["adjusted_gain"] == rep["totals"]["gain"] == a.realized
    rows = closed_lots(a)
    first = wash.analyse(a, rows)
    assert wash.analyse(a, rows) is first  # nothing changed: the cached result
    a.apply_fill(fill(99999, "buy", 1, "100", t=t0 + 30000), 1)
    assert wash.analyse(a, closed_lots(a)) is not first  # a new lot: worked out again


def test_huge_or_malformed_lot_quantities_are_rejected_not_server_errors():
    ex, feed, clock, a = exchange_with_lots("100")
    c = TestClient(create_app(ex, run_loop=False))
    for q in ("1e30", "1e999", 1e30, "NaN", "Infinity", "-1"):
        r = c.post(f"/api/accounts/{a}/orders", json={**sell(10), "lots": [{"lot_id": "L1", "qty": q}]})
        assert r.status_code == 422, (q, r.status_code, r.text)
        assert "lot L1" in r.json()["detail"]


def test_empty_or_wrongly_typed_lots_are_rejected_not_ignored():
    ex, feed, clock, a = exchange_with_lots("100")
    for bad, msg in (([], "lots is empty"), (0, "lots must be a list"), ({}, "lots must be a list"),
                     ("", "lots must be a list"), (False, "lots must be a list"), ([5], "lots must be a list")):
        with pytest.raises(OrderRejected, match=msg):
            ex.submit(a, {**sell(5), "lots": bad})
    with pytest.raises(OrderRejected, match="not on bracket orders"):
        ex.submit(a, {**sell(5), "lots": [], "take_profit": "200"})
    with pytest.raises(OrderRejected, match="not on one-cancels-other orders"):
        ex.submit(a, {"oco": [{**sell(5, type="limit", limit_price="200", tif="gtc"), "lots": []},
                              sell(5, type="stop", stop_price="50", tif="gtc")]})
    # null still means "no choice": the account's method closes the lot
    assert ex.submit(a, {**sell(5), "lots": None})[0].status == "filled"
    assert [(c["lot_id"], c["qty"], c["lot_method"]) for c in ex.account(a).closed] == [("L1", "5", "fifo")]
