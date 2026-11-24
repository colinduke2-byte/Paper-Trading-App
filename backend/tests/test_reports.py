"""Property tests for lot methods, CSV round trips and the reports API."""
import csv
import io
from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

from app.analytics import export
from app.analytics.trades import gains_report
from app.api.server import create_app
from app.core.models import Fill
from app.ledger.account import LOT_METHODS, Account
from app.ledger.ledger import position_account
from helpers import D, make_exchange, move
from test_ledger import fills_strategy


def fill(i, side, qty, price, symbol="ABC", fee="0"):
    return Fill(id=f"F{i}", order_id=None, account_id="A1", symbol=symbol, side=side, qty=D(str(qty)),
                price=D(str(price)), ts=float(i), fee=D(fee))


@settings(max_examples=300, deadline=None)
@given(fills_strategy, st.lists(st.sampled_from(LOT_METHODS), min_size=1, max_size=4))
def test_ledger_invariants_hold_for_every_lot_method(seq, methods):
    """The invariants of test_ledger.test_ledger_invariants, with the lot method changing mid-stream."""
    a = Account("A1", "t", "margin", D("1000000"), 0)
    for i, (side, qty, price, sym, fee) in enumerate(seq, 1):
        a.lot_method = methods[i % len(methods)]
        a.apply_fill(fill(i, side, qty, price, sym, str(fee)), 1)
        assert sum(v for _, v in a.ledger.transactions[-1].postings) == 0
    assert {k: v for k, v in a.ledger.recompute().items() if v} == {k: v for k, v in a.ledger.balances.items() if v}
    assert sum(a.ledger.balances.values()) == 0
    for sym in ("ABC", "XYZ"):
        assert a.ledger.balance(position_account(sym)) == a.cost_basis(sym)
        if a.position(sym) == 0:
            assert a.ledger.balance(position_account(sym)) == 0
        # every lot of a symbol has the same sign
        assert len({l.qty > 0 for l in a.lots.get(sym, [])}) <= 1
    basis = sum(a.cost_basis(s) for s in ("ABC", "XYZ"))
    assert a.cash == D("1000000") + a.realized - a.fees_paid - basis
    rep = gains_report(a)
    assert sum((r["gain"] for r in rep["rows"]), D(0)) == a.realized == rep["totals"]["gain"]
    assert all(r["proceeds"] - r["cost"] == r["gain"] for r in rep["rows"])
    b = Account.from_plain(a.to_plain())
    assert b.cash == a.cash and b.positions() == a.positions() and b.realized == a.realized
    assert b.lot_method == a.lot_method and gains_report(b)["totals"] == rep["totals"]


@settings(max_examples=100, deadline=None)
@given(fills_strategy)
def test_position_quantity_does_not_depend_on_lot_method(seq):
    accts = {m: Account("A1", "t", "margin", D("1000000"), 0) for m in LOT_METHODS}
    for m, a in accts.items():
        a.lot_method = m
        for i, (side, qty, price, sym, fee) in enumerate(seq, 1):
            a.apply_fill(fill(i, side, qty, price, sym, str(fee)), 1)
    pos = {tuple(sorted(a.positions().items())) for a in accts.values()}
    assert len(pos) == 1
    # Proceeds and partial-lot cost are rounded to the cent per lot, so which lots close can move cash
    # by at most a cent per closed lot; realised minus remaining basis moves with cash.
    slack = D("0.01") * (2 * max(len(a.closed) for a in accts.values()) + 1)
    cash = [a.cash for a in accts.values()]
    totals = [a.realized - sum(a.cost_basis(s) for s in ("ABC", "XYZ")) for a in accts.values()]
    assert max(cash) - min(cash) <= slack and max(totals) - min(totals) <= slack


# ---- CSV ------------------------------------------------------------------------------------
def read(text):
    return list(csv.DictReader(io.StringIO(text, newline="")))


@settings(max_examples=60, deadline=None)
@given(fills_strategy)
def test_csv_round_trips_exactly(seq):
    a = Account("A1", "t", "margin", D("1000000"), 0)
    for i, (side, qty, price, sym, fee) in enumerate(seq, 1):
        a.apply_fill(fill(i, side, qty, price, sym, str(fee)), 1)
    rows = read(export.fills_csv(a))
    assert [(r["id"], D(r["qty"]), D(r["price"]), D(r["fee"]), r["side"]) for r in rows] == \
        [(f.id, f.qty, f.price, f.fee, f.side) for f in a.fills]
    assert all(datetime.fromisoformat(r["time"]).timestamp() == f.ts for r, f in zip(rows, a.fills))
    led = read(export.ledger_csv(a))
    sums = {}
    for r in led:
        sums[r["transaction_id"]] = sums.get(r["transaction_id"], D(0)) + D(r["amount"])
        assert "E" not in r["amount"]
    assert set(sums.values()) == {D(0)}
    assert sum(D(r["amount"]) for r in led if r["account"] == "cash") == a.cash
    g = read(export.gains_csv(gains_report(a)))
    assert sum((D(r["gain"]) for r in g), D(0)) == a.realized


def test_csv_quoting_and_formula_guard():
    entries = [{"id": "J1", "target_type": "trade", "target_id": "C1", "created_at": 1.5, "updated_at": 2.0,
                "tags": ["breakout", "a,b"], "note": 'He said "sell", then\nleft'},
               {"id": "J2", "target_type": "order", "target_id": "O1", "created_at": 3.0, "updated_at": 3.0,
                "tags": [], "note": "=HYPERLINK(\"x\")"}]
    text = export.journal_csv(entries)
    assert text.endswith("\r\n")
    rows = read(text)
    assert rows[0]["note"] == 'He said "sell", then\nleft' and rows[0]["tags"] == "breakout;a,b"
    assert rows[0]["created_at"] == "1970-01-01T00:00:01.500+00:00"
    assert rows[1]["note"] == "'=HYPERLINK(\"x\")"
    assert export.cell(D("1E+2")) == "100" and export.cell(D("0.0000001")) == "0.0000001"
    try:
        export.cell(0.1)
        raise AssertionError("floats must be refused")
    except TypeError:
        pass


# ---- API ------------------------------------------------------------------------------------
def api_client(tmp=None):
    ex, feed, clock = make_exchange(tmp, ABC="100.00", MKTX="400.00")
    ex.create_account("Margin", "margin")
    return TestClient(create_app(ex, run_loop=False)), ex, feed, clock


def test_reports_api_flow(tmp_path):
    c, ex, feed, clock = api_client(str(tmp_path))
    aid = list(ex.accounts)[0]
    assert c.put(f"/api/accounts/{aid}/lot-method", json={"method": "average"}).status_code == 400
    assert c.put(f"/api/accounts/{aid}/lot-method", json={"method": "lifo"}).json()["method"] == "lifo"
    for q in (10, 10):
        assert c.post(f"/api/accounts/{aid}/orders", json={"symbol": "ABC", "side": "buy", "qty": q}).status_code == 200
        move(ex, feed, clock, "ABC", "110.00", seconds=61)
    move(ex, feed, clock, "MKTX", "440.00", seconds=61)
    o = c.post(f"/api/accounts/{aid}/orders", json={"symbol": "ABC", "side": "sell", "qty": 15}).json()
    a = ex.account(aid)
    # LIFO: 10 bought at 110.00 sold at 109.99 (-0.10), then 5 of the 100.00 lot (+49.95)
    assert a.realized == D("49.85") and D(a.closed[0]["open_price"]) == D("110")

    an = c.get(f"/api/accounts/{aid}/analytics").json()
    assert an["benchmark"]["symbol"] == "MKTX" and an["simulated"]
    assert abs(an["benchmark"]["total_return"] - 0.1) < 1e-9  # 400 -> 440
    assert an["trades"]["overall"]["trades"] == 2 and an["lot_method"] == "lifo"
    assert an["exposure"]["by_symbol"][0]["symbol"] == "ABC"
    assert len(an["account_curve"]) == len(an["benchmark"]["curve"]) >= 2
    assert c.get(f"/api/accounts/{aid}/analytics", params={"benchmark": "ZZZZ"}).status_code == 404
    assert c.get(f"/api/accounts/{aid}/analytics", params={"risk_free": "4"}).status_code == 400

    g = c.get(f"/api/accounts/{aid}/gains").json()
    assert abs(g["totals"]["gain"] - 49.85) < 1e-9 and g["lot_method"] == "lifo"
    months = c.get(f"/api/accounts/{aid}/statements").json()["months"]
    st = c.get(f"/api/accounts/{aid}/statements/{months[0]}").json()
    assert st["current"] and st["trade_count"] == 3 and abs(st["reconciliation_difference"]) < 1e-9
    assert c.get(f"/api/accounts/{aid}/statements/2099-01").status_code == 400
    assert c.get(f"/api/accounts/{aid}/statements/nonsense").status_code == 400
    page = c.get(f"/api/accounts/{aid}/statements/{months[0]}/html")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html") and "Monthly statement" in page.text

    for kind in ("fills", "orders", "ledger", "positions", "gains", "journal"):
        r = c.get(f"/api/accounts/{aid}/export/{kind}.csv")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        assert r.headers["content-disposition"].startswith(f'attachment; filename="paperdesk-{aid}-{kind}-')
        read(r.text)
    assert len(read(c.get(f"/api/accounts/{aid}/export/fills").text)) == 3
    assert read(c.get(f"/api/accounts/{aid}/export/positions.csv").text)[0]["qty"] == "5"
    assert c.get(f"/api/accounts/{aid}/export/secrets.csv").status_code == 404

    # journal
    base = f"/api/accounts/{aid}/journal"
    assert c.post(base, json={"target_type": "trade", "target_id": "C9", "note": "x"}).status_code == 404
    assert c.post(base, json={"target_type": "fill", "target_id": "C1", "note": "x"}).status_code == 400
    assert c.post(base, json={"target_type": "trade", "target_id": "C1"}).status_code == 400
    j = c.post(base, json={"target_type": "trade", "target_id": "C1", "note": "Chased it", "tags": "fomo, Fomo, late"}).json()
    assert j["tags"] == ["fomo", "late"]
    j2 = c.post(base, json={"target_type": "order", "target_id": o[0]["id"], "note": "Planned exit"}).json()
    assert [x["id"] for x in c.get(base).json()] == [j2["id"], j["id"]]
    assert [x["id"] for x in c.get(base, params={"tag": "FOMO"}).json()] == [j["id"]]
    assert c.patch(f"{base}/{j['id']}", json={"tags": ["lesson"]}).json()["tags"] == ["lesson"]
    assert c.patch(f"{base}/{j['id']}", json={"note": "", "tags": []}).status_code == 400
    assert c.delete(f"{base}/{j2['id']}").json()["ok"] and c.delete(f"{base}/{j2['id']}").status_code == 404
    assert len(read(c.get(f"/api/accounts/{aid}/export/journal").text)) == 1

    # everything persists with the account
    ex.save(force=True)
    from app.exchange import Exchange
    ex2 = Exchange(feed, clock, data_dir=str(tmp_path))
    assert ex2.load()
    b = ex2.account(aid)
    assert b.lot_method == "lifo" and [x["id"] for x in b.journal] == [j["id"]] and b.journal[0]["tags"] == ["lesson"]
