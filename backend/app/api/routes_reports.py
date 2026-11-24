"""Reports: analytics, realised gains, tax-lot method, statements, CSV export and the trade journal."""
from decimal import Decimal
from typing import Optional

from fastapi import Body, Query
from fastapi.responses import HTMLResponse, Response

from ..analytics import export, journal
from ..analytics.common import equity_points, external_flows, iso, month_bounds, months_between, parse_month
from ..analytics.returns import price_at, report as returns_report
from ..analytics.statement import monthly_statement, statement_html
from ..analytics.trades import closed_lots, exposure, gains_report, trade_stats
from ..core.money import model_to_price, parse as parse_num
from ..ledger.account import LOT_METHODS
from ..options.symbology import parse as parse_option
from ..core.errors import NotFound

DEFAULT_BENCHMARK = "MKTX"
CSV_KINDS = ("fills", "orders", "ledger", "positions", "gains", "journal")
LOT_METHOD_LABELS = {"fifo": "First in, first out", "lifo": "Last in, first out", "highest_cost": "Highest cost first"}


def price_history(ex, symbol: str) -> list:
    """[(ts, Decimal price)] in time order: daily closes, then one-minute closes, then the live price.

    A daily close is stamped at the end of its day and a minute close at the end of its minute, so a
    lookup at time t never uses a price from after t.
    """
    minutes = ex.feed.bars(symbol, "1m", 6000)
    first_minute = minutes[0]["time"] if minutes else float("inf")
    out = [(b["time"] + 86399, model_to_price(b["close"])) for b in ex.feed.bars(symbol, "1d", 5000)
           if b["time"] + 86399 < first_minute]
    out += [(b["time"] + 59, model_to_price(b["close"])) for b in minutes]
    q = ex.feed.quote(symbol)
    if q is not None:
        now = ex.now()
        out = [p for p in out if p[0] < now] + [(now, q.last)]
    return out


def register(app, ctx):
    def ex():
        return ctx.ex

    def plain(x):
        return ctx.plain(x)

    def curve(points) -> list:
        return [[t, float(v)] for t, v in points]  # chart data only

    def sectors() -> dict:
        return {s: i.get("sector", "Other") for s, i in ex().feed.symbols().items()}

    # ---- analytics --------------------------------------------------------------------------
    @app.get("/api/accounts/{aid}/analytics")
    def analytics(aid: str, benchmark: Optional[str] = None, risk_free: str = "0"):
        e = ex()
        a = e.account(aid)
        rf = parse_num(risk_free)
        if not Decimal("-1") < rf < Decimal("1"):
            raise ValueError("risk_free is an annual rate written as a fraction, for example 0.04")
        bench = (benchmark or "").upper().strip() or (DEFAULT_BENCHMARK if e.is_stock(DEFAULT_BENCHMARK) else "")
        if bench and not e.is_stock(bench):
            raise NotFound(f"unknown benchmark symbol {bench}")
        summ = e.summary(aid)
        series = equity_points(a, e.now(), summ["equity"])
        hist = price_history(e, bench) if bench else None
        r = returns_report(series, external_flows(a), hist, rf)
        index = r.pop("index")
        b = r["benchmark"]
        if b:
            b_index = b.pop("index")
            start_value = series[0][1]
            b["symbol"] = bench
            b["name"] = e.feed.symbols()[bench].get("name", bench)
            b["curve"] = [[t, float(start_value * v)] for t, v in b_index]
        lots = closed_lots(a)
        return plain({
            "account_id": aid, "simulated": True, "returns": r, "benchmark": b,
            "account_curve": curve(series), "index_curve": curve(index),
            "trades": trade_stats(lots),
            "exposure": exposure(e.positions(aid), sectors(), summ["equity"]),
            "summary": {k: summ[k] for k in ("equity", "cash", "realized_pnl", "unrealized_pnl", "fees", "starting_cash")},
            "lot_method": a.lot_method,
        })

    @app.get("/api/accounts/{aid}/gains")
    def gains(aid: str, month: Optional[str] = None):
        a = ex().account(aid)
        start = end = None
        if month:
            start, end = month_bounds(*parse_month(month))
        return plain(gains_report(a, start, end))

    # ---- tax-lot method ----------------------------------------------------------------------
    @app.get("/api/accounts/{aid}/lot-method")
    def get_lot_method(aid: str):
        return {"method": ex().account(aid).lot_method, "options": LOT_METHOD_LABELS}

    @app.put("/api/accounts/{aid}/lot-method")
    def put_lot_method(aid: str, body: dict = Body(...)):
        e = ex()
        a = e.account(aid)
        method = str(body.get("method", "")).lower()
        if method not in LOT_METHODS:
            raise ValueError(f"lot method must be one of {', '.join(LOT_METHODS)}")
        a.lot_method = method
        e.dirty = True
        return {"method": method, "options": LOT_METHOD_LABELS,
                "note": "The method applies to closing trades from now on; lots already closed keep their method."}

    # ---- statements --------------------------------------------------------------------------
    def _statement(aid: str, month: str) -> dict:
        e = ex()
        a = e.account(aid)
        year, mon = parse_month(month)
        start, end = month_bounds(year, mon)
        now = e.now()
        current = end > now
        marks = {}
        for f in a.fills:
            sym = f.symbol
            if sym in marks or f.ts >= end:
                continue
            if current:
                marks[sym] = e.mark(sym)
            elif parse_option(sym) is None and e.is_stock(sym):
                marks[sym] = price_at(price_history(e, sym), end - 1)
        marks = {s: p for s, p in marks.items() if p is not None}
        live = e.summary(aid)["equity"] if current else None
        return monthly_statement(a, year, mon, now, live, marks)

    @app.get("/api/accounts/{aid}/statements")
    def statements(aid: str):
        e = ex()
        a = e.account(aid)
        return {"months": months_between(a.created_at, e.now())}

    @app.get("/api/accounts/{aid}/statements/{month}")
    def statement(aid: str, month: str):
        return plain(_statement(aid, month))

    @app.get("/api/accounts/{aid}/statements/{month}/html", response_class=HTMLResponse)
    def statement_page(aid: str, month: str):
        return HTMLResponse(statement_html(_statement(aid, month)))

    # ---- CSV export --------------------------------------------------------------------------
    @app.get("/api/accounts/{aid}/export/{kind}")
    def export_csv(aid: str, kind: str):
        e = ex()
        a = e.account(aid)
        kind = kind.removesuffix(".csv")
        if kind not in CSV_KINDS:
            raise NotFound(f"no export named {kind}; choose one of {', '.join(CSV_KINDS)}")
        if kind == "fills":
            body = export.fills_csv(a)
        elif kind == "orders":
            body = export.orders_csv(e.orders(aid))
        elif kind == "ledger":
            body = export.ledger_csv(a)
        elif kind == "positions":
            body = export.positions_csv(e.positions(aid))
        elif kind == "gains":
            body = export.gains_csv(gains_report(a))
        else:
            body = export.journal_csv(journal.entries(a))
        stamp = iso(e.now())[:10].replace("-", "")
        filename = f"paperdesk-{aid}-{kind}-{stamp}.csv"
        return Response(body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    # ---- trade journal -----------------------------------------------------------------------
    def _check_target(e, a, target_type: str, target_id: str) -> None:
        if target_type == "order":
            o = e.engine.orders.get(target_id)
            if o is None or o.account_id != a.id:
                raise NotFound(f"no order {target_id} in this account")
        elif target_type == "trade":
            if target_id not in {c["id"] for c in closed_lots(a)}:
                raise NotFound(f"no closed trade {target_id} in this account")

    @app.get("/api/accounts/{aid}/journal")
    def list_journal(aid: str, target_type: Optional[str] = None, target_id: Optional[str] = None,
                     tag: Optional[str] = None):
        return journal.entries(ex().account(aid), target_type, target_id, tag)

    @app.post("/api/accounts/{aid}/journal")
    def add_journal(aid: str, body: dict = Body(...)):
        e = ex()
        a = e.account(aid)
        target_type, target_id = str(body.get("target_type", "")), str(body.get("target_id", ""))
        if target_type not in journal.TARGETS:
            raise ValueError("an entry must be attached to an 'order' or a 'trade'")
        _check_target(e, a, target_type, target_id)
        entry = journal.add(a, target_type, target_id, body.get("note"), body.get("tags"), e.now())
        e.dirty = True
        return entry

    @app.patch("/api/accounts/{aid}/journal/{jid}")
    def edit_journal(aid: str, jid: str, body: dict = Body(...)):
        e = ex()
        entry = journal.update(e.account(aid), jid, e.now(), body.get("note"), body.get("tags"))
        e.dirty = True
        return entry

    @app.delete("/api/accounts/{aid}/journal/{jid}")
    def delete_journal(aid: str, jid: str):
        e = ex()
        journal.delete(e.account(aid), jid)
        e.dirty = True
        return {"ok": True}
