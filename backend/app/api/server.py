"""REST and WebSocket API. Single local user; binds to localhost by default."""
import asyncio
import contextlib
import os
from dataclasses import fields, is_dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core import calendar as cal
from ..exchange import Exchange, OrderRejected
from ..marketdata.feed import TIMEFRAMES
from ..marketdata.synthetic import SyntheticFeed
from ..options import chain as optchain
from ..options.symbology import describe, parse as parse_option


def plain(obj: Any) -> Any:
    """Convert to JSON-safe values. Decimals become numbers for display only."""
    if isinstance(obj, Decimal):
        return float(obj)
    if is_dataclass(obj):
        return {f.name: plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [plain(v) for v in obj]
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def quote_row(ex: Exchange, symbol: str) -> Optional[dict]:
    q = ex.quote(symbol)
    if q is None:
        return None
    row = plain(q)
    if q.prev_close:
        row["change"] = float(q.last - q.prev_close)
        row["change_pct"] = float((q.last - q.prev_close) / q.prev_close * 100)
    return row


def order_row(o) -> dict:
    row = plain(o)
    row["description"] = describe(o.symbol)
    return row


def default_exchange() -> Exchange:
    import time
    data_dir = os.environ.get("PAPERDESK_DATA", os.path.join(os.getcwd(), "data"))
    mode = os.environ.get("PAPERDESK_SESSIONS", "always")
    feed = SyntheticFeed(seed=int(os.environ.get("PAPERDESK_SEED", "7")), now=time.time(), always_open=mode == "always")
    ex = Exchange(feed, data_dir=data_dir, session_mode=mode)
    if not ex.load() or not ex.accounts:
        ex.create_account("Margin account", "margin", Decimal("100000"))
        ex.create_account("Cash account", "cash", Decimal("25000"))
    if not ex.watchlists:
        ex.watchlists = {"Main": ["MKTX", "TECX", "NVLX", "ORBT", "PXLR", "AUTO", "FNBK", "ENRG", "RETL", "STRM"]}
    return ex


def create_app(ex: Optional[Exchange] = None, run_loop: bool = True, tick_seconds: float = 1.0) -> FastAPI:
    ex = ex or default_exchange()
    clients: dict[WebSocket, dict] = {}

    def status() -> dict:
        now = ex.now()
        return {"time": now, "session": ex.session(now), "session_mode": ex.session_mode,
                "feed": ex.feed.name, "simulated_data": ex.feed.simulated, "delay_seconds": ex.feed.delay_seconds,
                "paper": True}

    async def broadcast() -> None:
        events = ex.drain_events()
        st = status()
        for ws, sub in list(clients.items()):
            msg = {"type": "tick", "status": st,
                   "quotes": {s: r for s in sub["symbols"] if (r := quote_row(ex, s))}}
            acct = sub.get("account")
            if acct in ex.accounts:
                msg["account"] = plain(ex.summary(acct))
                msg["positions"] = plain(ex.positions(acct))
            msg["events"] = [plain(e) for e in events if e.get("account_id") in (None, acct)]
            try:
                await ws.send_json(msg)
            except Exception:
                clients.pop(ws, None)

    async def loop() -> None:
        while True:
            try:
                ex.tick()
                await broadcast()
                ex.save()
            except Exception as e:  # keep the market running; report the fault
                print(f"tick error: {e!r}")
            await asyncio.sleep(tick_seconds)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(loop()) if run_loop else None
        yield
        if task:
            task.cancel()
        ex.save(force=True)

    app = FastAPI(title="PaperDesk", lifespan=lifespan)
    app.state.exchange = ex

    @app.exception_handler(OrderRejected)
    async def _rejected(_, exc):
        return JSONResponse({"detail": str(exc), "rejected": True}, status_code=422)

    @app.exception_handler(KeyError)
    async def _missing(_, exc):
        return JSONResponse({"detail": str(exc).strip("'\"")}, status_code=404)

    @app.exception_handler(ValueError)
    async def _bad(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # ---- market ----------------------------------------------------------
    @app.get("/api/status")
    def get_status():
        return status()

    @app.get("/api/symbols")
    def symbols(q: str = ""):
        q = q.upper().strip()
        rows = [{"symbol": s, "name": i["name"], "sector": i["sector"], "type": i.get("type", "stock")}
                for s, i in ex.feed.symbols().items()
                if not q or q in s or q in i["name"].upper()]
        return sorted(rows, key=lambda r: (not r["symbol"].startswith(q), r["symbol"]))

    @app.get("/api/quotes")
    def quotes(symbols: str = Query("")):
        out = {}
        for s in [x.strip().upper() for x in symbols.split(",") if x.strip()][:200]:
            row = quote_row(ex, s)
            if row:
                out[s] = row
        return out

    def _stock(symbol: str) -> str:
        symbol = symbol.upper()
        if not ex.is_stock(symbol):
            raise KeyError(f"unknown symbol {symbol}")
        return symbol

    @app.get("/api/bars/{symbol}")
    def bars(symbol: str, tf: str = "1d", limit: int = Query(500, ge=1, le=5000)):
        if tf not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
        return ex.feed.bars(_stock(symbol), tf, limit)

    @app.get("/api/depth/{symbol}")
    def depth(symbol: str):
        return plain(ex.feed.depth(_stock(symbol)))

    @app.get("/api/trades/{symbol}")
    def trades(symbol: str, limit: int = Query(50, ge=1, le=200)):
        return plain(ex.feed.trades(_stock(symbol), limit))

    @app.get("/api/fundamentals/{symbol}")
    def fundamentals(symbol: str):
        symbol = _stock(symbol)
        info, q = ex.feed.symbols()[symbol], ex.feed.quote(symbol)
        last = float(q.last)
        daily = ex.feed.bars(symbol, "1d", 252)
        eps, div = info.get("eps", 0), info.get("dividend", 0)
        vols = [b["volume"] for b in daily[-30:]]
        return {"symbol": symbol, "name": info["name"], "sector": info["sector"], "type": info.get("type", "stock"),
                "market_cap": last * info.get("shares_outstanding", 0), "shares_outstanding": info.get("shares_outstanding"),
                "eps": eps, "pe": last / eps if eps and eps > 0 else None, "dividend": div,
                "dividend_yield_pct": div / last * 100 if last else None,
                "high_52w": max([b["high"] for b in daily] + [float(q.high)]),
                "low_52w": min([b["low"] for b in daily] + [float(q.low)]),
                "avg_volume_30d": sum(vols) / len(vols) if vols else None,
                "historical_volatility": info.get("vol"), "simulated": ex.feed.simulated}

    @app.get("/api/movers")
    def movers():
        rows = [r for s in ex.feed.symbols() if (r := quote_row(ex, s)) and "change_pct" in r]
        for r in rows:
            r.update(name=ex.feed.symbols()[r["symbol"]]["name"], sector=ex.feed.symbols()[r["symbol"]]["sector"])
        rows.sort(key=lambda r: r["change_pct"], reverse=True)
        return {"gainers": rows[:5], "losers": rows[::-1][:5],
                "active": sorted(rows, key=lambda r: r["volume"], reverse=True)[:5], "all": rows}

    @app.get("/api/screener")
    def screener(sector: str = "", min_price: Optional[float] = None, max_price: Optional[float] = None,
                 min_change_pct: Optional[float] = None, max_change_pct: Optional[float] = None,
                 min_volume: Optional[float] = None, max_pe: Optional[float] = None,
                 min_dividend_yield: Optional[float] = None):
        out = []
        for s, info in ex.feed.symbols().items():
            r = quote_row(ex, s)
            if not r:
                continue
            eps, div = info.get("eps", 0), info.get("dividend", 0)
            pe = r["last"] / eps if eps and eps > 0 else None
            dy = div / r["last"] * 100
            if sector and info["sector"].lower() != sector.lower():
                continue
            if min_price is not None and r["last"] < min_price or max_price is not None and r["last"] > max_price:
                continue
            if min_change_pct is not None and r.get("change_pct", 0) < min_change_pct:
                continue
            if max_change_pct is not None and r.get("change_pct", 0) > max_change_pct:
                continue
            if min_volume is not None and r["volume"] < min_volume:
                continue
            if max_pe is not None and (pe is None or pe > max_pe):
                continue
            if min_dividend_yield is not None and dy < min_dividend_yield:
                continue
            out.append({"symbol": s, "name": info["name"], "sector": info["sector"], "last": r["last"],
                        "change_pct": r.get("change_pct"), "volume": r["volume"], "pe": pe, "dividend_yield_pct": dy,
                        "market_cap": r["last"] * info.get("shares_outstanding", 0)})
        return sorted(out, key=lambda r: r["market_cap"], reverse=True)

    # ---- options -----------------------------------------------------------
    @app.get("/api/options/expirations/{symbol}")
    def expirations(symbol: str):
        _stock(symbol)
        today = cal.ny_date(ex.now())
        return [{"date": d.isoformat(), "dte": (d - today).days} for d in optchain.expirations(today)
                if cal.close_ts(d) > ex.now()]

    @app.get("/api/options/chain/{symbol}")
    def option_chain(symbol: str, expiry: str):
        symbol = _stock(symbol)
        try:
            exp = date.fromisoformat(expiry)
        except ValueError:
            raise ValueError("expiry must be YYYY-MM-DD")
        if exp not in optchain.expirations(cal.ny_date(ex.now())):
            raise KeyError("expiry is not listed")
        q = ex.feed.quote(symbol)
        return {"underlying": symbol, "expiry": expiry, "spot": float(q.last), "simulated": True,
                "rows": plain(optchain.chain(symbol, exp, float(q.last), ex.feed.base_vol(symbol), ex.now()))}

    # ---- accounts ------------------------------------------------------------
    @app.get("/api/accounts")
    def accounts():
        return [plain(ex.summary(a)) for a in ex.accounts]

    @app.post("/api/accounts")
    def create_account(body: dict = Body(...)):
        from ..core.money import parse as parse_num
        acct = ex.create_account(str(body.get("name", "")), body.get("type", "margin"),
                                 parse_num(body.get("starting_cash", 100000)))
        return plain(ex.summary(acct.id))

    @app.post("/api/accounts/{aid}/reset")
    def reset_account(aid: str, body: dict = Body(default={})):
        from ..core.money import parse as parse_num
        cash = parse_num(body["starting_cash"]) if body.get("starting_cash") else None
        if cash is not None and cash <= 0:
            raise ValueError("starting cash must be positive")
        ex.reset_account(aid, cash)
        return plain(ex.summary(aid))

    @app.delete("/api/accounts/{aid}")
    def delete_account(aid: str):
        ex.delete_account(aid)
        return {"ok": True}

    @app.get("/api/accounts/{aid}")
    def account(aid: str):
        return plain(ex.summary(aid))

    @app.get("/api/accounts/{aid}/positions")
    def positions(aid: str):
        return plain(ex.positions(aid))

    @app.get("/api/accounts/{aid}/orders")
    def orders(aid: str, status: str = "all"):
        return [order_row(o) for o in ex.orders(aid, status)]

    @app.get("/api/accounts/{aid}/fills")
    def fills(aid: str, limit: int = Query(500, ge=1, le=5000)):
        rows = [dict(plain(f), description=describe(f.symbol)) for f in ex.account(aid).fills[-limit:]]
        return rows[::-1]

    @app.get("/api/accounts/{aid}/ledger")
    def ledger(aid: str, limit: int = Query(500, ge=1, le=5000)):
        txns = ex.account(aid).ledger.transactions[-limit:]
        return [{"id": t.id, "ts": t.ts, "kind": t.kind, "memo": t.memo,
                 "postings": [[a, float(v)] for a, v in t.postings]} for t in txns][::-1]

    @app.get("/api/accounts/{aid}/closed")
    def closed(aid: str):
        return [dict(c, description=describe(c["symbol"]), qty=float(c["qty"]), open_price=float(c["open_price"]),
                     close_price=float(c["close_price"]), pnl=float(c["pnl"])) for c in ex.account(aid).closed][::-1]

    @app.get("/api/accounts/{aid}/performance")
    def performance(aid: str):
        perf = ex.performance(aid)
        perf["equity_curve"] = [[t, float(e)] for t, e in perf["equity_curve"]]
        return plain(perf)

    @app.post("/api/accounts/{aid}/orders/preview")
    def preview(aid: str, body: dict = Body(...)):
        return plain(ex.preview(aid, body))

    @app.post("/api/accounts/{aid}/orders")
    def submit(aid: str, body: dict = Body(...)):
        return [order_row(o) for o in ex.submit(aid, body)]

    @app.delete("/api/accounts/{aid}/orders")
    def cancel_all(aid: str):
        ex.account(aid)
        return {"canceled": ex.cancel_all(aid)}

    @app.delete("/api/accounts/{aid}/orders/{oid}")
    def cancel(aid: str, oid: str):
        return order_row(ex.cancel(aid, oid))

    @app.patch("/api/accounts/{aid}/orders/{oid}")
    def replace(aid: str, oid: str, body: dict = Body(...)):
        return order_row(ex.replace(aid, oid, body))

    # ---- watchlists and alerts ---------------------------------------------------
    @app.get("/api/watchlists")
    def watchlists():
        return ex.watchlists

    @app.put("/api/watchlists/{name}")
    def put_watchlist(name: str, body: list = Body(...)):
        syms = []
        for s in body[:100]:
            s = str(s).upper().strip()
            if ex.is_stock(s) and s not in syms:
                syms.append(s)
        ex.watchlists[name[:30]] = syms
        ex.dirty = True
        return ex.watchlists

    @app.delete("/api/watchlists/{name}")
    def delete_watchlist(name: str):
        ex.watchlists.pop(name, None)
        ex.dirty = True
        return ex.watchlists

    @app.get("/api/alerts")
    def alerts():
        return ex.alerts

    @app.post("/api/alerts")
    def add_alert(body: dict = Body(...)):
        return ex.add_alert(str(body.get("symbol", "")), body.get("op"), body.get("price"))

    @app.delete("/api/alerts/{alert_id}")
    def remove_alert(alert_id: str):
        ex.remove_alert(alert_id)
        return {"ok": True}

    # ---- streaming --------------------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        clients[ws] = {"symbols": [], "account": None}
        try:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict):
                    if isinstance(msg.get("symbols"), list):
                        clients[ws]["symbols"] = [str(s).upper() for s in msg["symbols"][:300]]
                    if "account" in msg:
                        clients[ws]["account"] = msg["account"]
        except (WebSocketDisconnect, Exception):
            clients.pop(ws, None)

    # ---- static UI -----------------------------------------------------------------
    dist = os.environ.get("PAPERDESK_UI", os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "dist"))
    if os.path.isdir(dist):
        app.mount("/assets", StaticFiles(directory=os.path.join(dist, "assets")), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def ui(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(os.path.join(dist, "index.html"))

    return app


def app_factory() -> FastAPI:
    return create_app()
