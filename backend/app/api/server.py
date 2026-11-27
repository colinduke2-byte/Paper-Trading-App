"""REST and WebSocket API. Single local user; binds to localhost by default."""
import asyncio
import contextlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, is_dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core import calendar as cal
from ..core.locking import Busy, StateLock
from ..exchange import Exchange, OrderRejected
from ..marketdata.feed import TIMEFRAMES
from ..marketdata.labels import price_source
from ..marketdata.synthetic import SyntheticFeed
from ..options import chain as optchain
from ..options.symbology import describe, parse as parse_option
from ..core.errors import NotFound
from . import ui_build
from . import locking
from .locking import unlocked

TICK_WAIT = 1.0  # seconds the tick waits for the exchange lock before it skips a turn and says who holds it
MARKET_DATA = "market data: reads only the feed, which may ask the provider"


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
    note = ex.feed.quote_note(symbol) if ex.is_stock(symbol) else None
    if note:  # a live feed explains a one-sided book or a price that is not from today
        row.update(stale=note["stale"], note=note["note"])
    return row


def symbol_rows(ex: Exchange, q: str, limit: int) -> list:
    info = ex.feed.symbols()
    row = lambda s: {"symbol": s, "name": info[s]["name"], "sector": info[s].get("sector", ""),
                     "type": info[s].get("type", "stock")}
    q = q.upper().strip()
    if not q:
        # Called without the exchange lock: watchlists, positions and orders only order the list, so a
        # change while they are read just drops that ordering (alphabetical instead); nothing is written.
        try:
            first = [s for wl in list(ex.watchlists.values()) for s in list(wl)]
            for a in list(ex.accounts.values()):
                first += [(parse_option(s).underlying if parse_option(s) else s) for s in list(a.lots)]
            first += [o.symbol for o in ex.engine.active_orders()]
        except RuntimeError:  # a dict or list changed size while it was read
            first = []
        picked = [s for s in dict.fromkeys(first) if s in info][:limit]
        seen = set(picked)
        rest = sorted(s for s in info if s not in seen)[:max(limit - len(picked), 0)]
        return [row(s) for s in picked + rest]
    # exact symbol, then symbols starting with q, then symbols containing it, then company names
    ranked = []
    for s, i in info.items():
        name = (i.get("name") or "").upper()
        if s == q:
            rank = 0
        elif s.startswith(q):
            rank = 1
        elif q in s:
            rank = 2
        elif name.startswith(q):
            rank = 3
        elif q in name:
            rank = 4
        else:
            continue
        ranked.append((rank, len(s), s))
    ranked.sort()
    return [row(s) for _, _, s in ranked[:limit]]


def order_row(o) -> dict:
    row = plain(o)
    row["description"] = describe(o.symbol)
    return row


def default_exchange() -> Exchange:
    from ..core import envfile
    from ..marketdata.config import attach, select_feed
    envfile.load_settings()  # the .env file first, so every setting below honours it
    data_dir = os.environ.get("PAPERDESK_DATA", os.path.join(os.getcwd(), "data"))
    # Feed selection (docs/DATA_PROVIDERS.md): reads .env, uses Alpaca when keys exist, else synthetic.
    sel = select_feed(time.time(), data_dir, synthetic_seed=int(os.environ.get("PAPERDESK_SEED", "7")))
    ex = Exchange(sel.feed, data_dir=sel.data_dir, session_mode=sel.session_mode)
    attach(sel, ex)
    if not ex.load() or not ex.accounts:
        ex.create_account("Margin account", "margin", Decimal("100000"))
        ex.create_account("Cash account", "cash", Decimal("25000"))
    if not ex.watchlists:
        ex.watchlists = {"Main": sel.watchlist or ["MKTX", "TECX", "NVLX", "ORBT", "PXLR", "AUTO", "FNBK", "ENRG", "RETL", "STRM"]}
    return ex


class AppContext:
    """Shared by the core routes and every routes_*.py module.

    ctx.ex is the ACTIVE exchange: the live one, or a replay sandbox while a replay runs. Look it up
    on every request (never keep a reference across requests). ctx.plain converts to JSON-safe values,
    ctx.clients holds the WebSocket subscriptions. ctx.live is always the live exchange and
    ctx.replay the running replay controller, or None.

    ctx.lock is the exchange lock (docs/CONCURRENCY.md). Every plain `def` route under /api already
    runs while holding it. Code that runs elsewhere (a route marked @unlocked, a background thread)
    wraps each piece of work on exchange state in `with ctx.locked():` and does nothing slow inside.
    """

    def __init__(self, ex: Exchange, clients: dict, app: Optional[FastAPI] = None):
        self.live = ex
        self.ex = ex
        self.replay = None
        self.plain = plain
        self.clients = clients
        self.app = app
        self.lock = StateLock()  # covers the active exchange, the live exchange, mode switches and snapshots
        self.metrics = {"turns": 0, "skipped": 0, "errors": 0, "lock_wait_max": 0.0, "turn_max": 0.0,
                        "lock_wait_total": 0.0, "turn_total": 0.0}
        self._interest: list = []

    @property
    def mode(self) -> str:
        return "replay" if self.replay is not None else "live"

    def locked(self, label: Optional[str] = None):
        """`with ctx.locked():` holds the exchange lock, or raises Busy (HTTP 503) if it stays taken."""
        return self.lock.hold(locking.wait_seconds(), label=label)

    def live_interest(self) -> list:
        """Symbols the live exchange needs prices for. Called by the live feed's poller thread, which
        must not walk exchange state on its own: the list is read under the lock, and when the lock is
        busy the previous list is returned."""
        from ..marketdata.config import wanted_symbols
        if self.lock.acquire(0.5, label="feed interest"):
            try:
                self._interest = wanted_symbols(self.live)
            finally:
                self.lock.release()
        return list(self._interest)

    def _publish(self) -> None:
        if self.app is not None:
            self.app.state.exchange = self.ex

    def enter_replay(self, controller) -> None:
        """Make the replay sandbox the active exchange. Live state is saved first and left untouched."""
        with self.locked("replay start"):
            if self.replay is not None:
                raise RuntimeError("a replay is already running; exit it first")
            files = self.live.save_plan(force=True)
            self.live.drain_events()
            controller.guard = self.lock
            self.replay = controller
            self.ex = controller.ex
            self._publish()
        try:
            self.live.write_plan(files)  # disk I/O after the lock is released
        except OSError as e:  # the replay is running already; live stays dirty and is saved after the exit
            print(f"replay start: the live state could not be written yet: {e!r}")

    def exit_replay(self) -> None:
        """Return to the live exchange exactly as it was when the replay started."""
        with self.locked("replay exit"):
            if self.replay is None:
                raise RuntimeError("no replay is running")
            controller, self.replay = self.replay, None
            self.ex = self.live
            controller.closed = controller.paused = True  # a step or seek still running stops at its next slice
            files = controller.ex.save_plan(force=True)
            self._publish()
        try:
            controller.ex.write_plan(files)
        except OSError:
            pass


def create_app(ex: Optional[Exchange] = None, run_loop: bool = True, tick_seconds: float = 1.0,
               replay_tick_seconds: float = 0.25) -> FastAPI:
    clients: dict[WebSocket, dict] = {}
    ctx = AppContext(ex or default_exchange(), clients)
    del ex  # every route looks the active exchange up through ctx.ex
    dist = os.environ.get("PAPERDESK_UI", os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "dist"))
    ui_stale = os.path.isdir(dist) and ui_build.check(dist)  # silent without the sources (ZIP, Docker)

    def status() -> dict:
        ex = ctx.ex
        now = ex.now()
        rp = ctx.replay
        return {"time": now, "session": ex.session(now), "session_mode": ex.session_mode,
                "feed": ex.feed.name, "simulated_data": ex.feed.simulated, "delay_seconds": ex.feed.delay_seconds,
                "paper": True, **plain(ex.feed.status_info()), "price_note": price_source(ex.feed)["price_note"],
                "mode": ctx.mode, "replay": rp.status() if rp is not None else None, "ui_stale": ui_stale,
                # Alpaca was configured but failed at start-up, so the live exchange runs on simulated data
                "live_unavailable": getattr(ctx.live.feed, "live_unavailable", None),
                "day_roll_waiting": ex.lifecycle.waiting}  # set while the start of a day waits for corporate actions

    def messages(ex: Exchange) -> list:
        """One tick message per WebSocket client, built while the caller holds the exchange lock."""
        events = ex.drain_events()
        st = status()
        out = []
        for ws, sub in list(clients.items()):
            try:
                msg = {"type": "tick", "status": st,
                       "quotes": {s: r for s in list(sub["symbols"]) if (r := quote_row(ex, s))}}
                acct = sub.get("account")
                if isinstance(acct, str) and acct in ex.accounts:
                    msg["account"] = plain(ex.summary(acct))
                    msg["positions"] = plain(ex.positions(acct))
                msg["events"] = [plain(e) for e in events if e.get("account_id") in (None, acct)]
            except Exception as e:  # one client's fault must not cost the others their message
                metrics["errors"] += 1
                print(f"broadcast error: {e!r}")
                continue
            out.append((ws, msg))
        return out

    async def broadcast(out: list) -> None:
        """Send what messages() built. Never called with the exchange lock held."""
        for ws, msg in out:
            try:
                await ws.send_json(msg)
            except Exception:
                clients.pop(ws, None)

    # The tick runs on its own thread, so the event loop never waits for the exchange lock, and state
    # files are written by a second thread, so no thread writes to disk while holding it.
    work = {"tick": None, "save": None, "saving": None, "stuck": False}
    metrics = ctx.metrics

    def turn(elapsed: float) -> tuple:
        """One turn of the market: tick, take a snapshot to save, build the broadcast. All under the lock."""
        asked = time.monotonic()
        if not ctx.lock.acquire(TICK_WAIT, urgent=True, label="tick"):
            metrics["skipped"] += 1
            who = ctx.lock.holder()
            raise Busy("the exchange lock is held" + (f" by {who[0]} for {who[1]:.1f} s" if who else ""))
        try:
            got = time.monotonic()
            ex, rp = ctx.ex, ctx.replay
            if rp is not None:
                rp.advance(elapsed)  # ticks the sandbox once per simulated second
            else:
                ex.tick()
            try:
                out = messages(ex)
            except Exception as e:  # a broadcast fault must not cost the snapshot of a tick that already ran
                metrics["errors"] += 1
                print(f"broadcast error: {e!r}")
                out = []
            idle = work["saving"] is None or work["saving"].done()
            # The last statement that can fail: once save_plan() has reset `dirty`, the plan must reach
            # the caller, or the changes it holds are not written until the next change.
            files = ex.save_plan() if idle else []  # still dirty next turn when the last write is not done
        finally:
            ctx.lock.release()
        wait, took = got - asked, time.monotonic() - got
        metrics["turns"] += 1
        metrics["lock_wait_total"] += wait
        metrics["turn_total"] += took
        metrics["lock_wait_max"] = max(metrics["lock_wait_max"], wait)
        metrics["turn_max"] = max(metrics["turn_max"], took)
        return ex, files, out

    def write(ex: Exchange, files: list) -> None:
        try:
            ex.write_plan(files)
        except Exception as e:  # the exchange stays dirty, so the next turn saves again
            print(f"save error: {e!r}")

    async def loop() -> None:
        running = asyncio.get_running_loop()
        last = time.monotonic()  # wall time only paces replay; business logic reads ex.now()
        while True:
            try:
                wall = time.monotonic()
                # Counted once, also when the turn raises after it advanced (a replay must not run fast)
                elapsed, last = wall - last, wall
                ex, files, out = await running.run_in_executor(work["tick"], turn, elapsed)
                work["stuck"] = False
                if files:
                    work["saving"] = work["save"].submit(write, ex, files)
                await broadcast(out)
            except Busy as e:  # a request holds the lock for too long: skip the turn, try again next time
                last -= elapsed  # nothing advanced: the next turn catches up on this time
                if not work["stuck"]:
                    print(f"tick skipped: {e}")
                work["stuck"] = True
            except Exception as e:  # keep the market running; report the fault
                metrics["errors"] += 1
                print(f"tick error: {e!r}")
            await asyncio.sleep(min(tick_seconds, replay_tick_seconds) if ctx.replay is not None else tick_seconds)

    def shutdown_save() -> None:
        if not ctx.lock.acquire(5.0, urgent=True, label="shutdown"):
            print("shutdown: the exchange lock is still held, so the last changes were not saved")
            return
        try:
            todo = [(ex, ex.save_plan(force=True)) for ex in dict.fromkeys((ctx.ex, ctx.live))]
        finally:
            ctx.lock.release()
        for ex, files in todo:
            ex.write_plan(files)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        work["tick"] = ThreadPoolExecutor(1, thread_name_prefix="paperdesk-tick")
        work["save"] = ThreadPoolExecutor(1, thread_name_prefix="paperdesk-save")
        task = asyncio.create_task(loop()) if run_loop else None
        yield
        if task:
            task.cancel()
        running = asyncio.get_running_loop()
        for pool in (work["tick"], work["save"]):  # let a turn or a write that has started finish
            await running.run_in_executor(None, pool.shutdown)
        await running.run_in_executor(None, shutdown_save)

    app = FastAPI(title="PaperDesk", lifespan=lifespan)
    ctx.app = app
    app.state.exchange = ctx.ex
    app.state.ctx = ctx
    locking.install(app, ctx)  # every route registered from here on runs under ctx.lock unless marked @unlocked
    app.add_exception_handler(Busy, locking.busy_response)

    @app.exception_handler(OrderRejected)
    async def _rejected(_, exc):
        return JSONResponse({"detail": str(exc), "rejected": True}, status_code=422)

    @app.exception_handler(NotFound)
    async def _missing(_, exc):
        # only deliberate "no such thing" lookups: a stray KeyError is a bug, not a 404
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def _bad(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def _bug(request, exc):
        # Bad input is answered above with a 4xx; anything that reaches here is a bug. Answer in JSON
        # (the UI shows "detail") and keep the traceback in the server log, not in the response.
        import logging
        logging.getLogger("paperdesk.api").error("unhandled error on %s %s", request.method, request.url.path,
                                                 exc_info=exc)
        return JSONResponse({"detail": f"PaperDesk hit an internal error ({type(exc).__name__}). This is a bug; "
                                       "the server log has the details."}, status_code=500)

    # ---- market ----------------------------------------------------------
    @app.get("/api/status")
    def get_status():
        ex = ctx.ex
        return status()

    @app.get("/api/symbols")
    @unlocked(MARKET_DATA)
    def symbols(q: str = "", limit: int = Query(50, ge=1, le=500)):
        """Search every symbol; return at most `limit` rows. An empty query gives a short useful list
        (watchlists, positions and open orders first, then the rest alphabetically), not every listing.
        Runs without the exchange lock (symbol search must answer while it is busy); see symbol_rows."""
        ex = ctx.ex
        return symbol_rows(ex, q, limit)

    @app.get("/api/quotes")
    @unlocked(MARKET_DATA)
    def quotes(symbols: str = Query("")):
        ex = ctx.ex
        out = {}
        for s in [x.strip().upper() for x in symbols.split(",") if x.strip()][:200]:
            row = quote_row(ex, s)
            if row:
                out[s] = row
        return out

    def _stock(ex: Exchange, symbol: str) -> str:
        symbol = symbol.upper()
        if not ex.is_stock(symbol):
            raise NotFound(f"unknown symbol {symbol}")
        return symbol

    @app.get("/api/bars/{symbol}")
    @unlocked(MARKET_DATA)
    def bars(symbol: str, tf: str = "1d", limit: int = Query(500, ge=1, le=5000)):
        ex = ctx.ex
        if tf not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
        return ex.feed.bars(_stock(ex, symbol), tf, limit)

    @app.get("/api/depth/{symbol}")
    @unlocked(MARKET_DATA)
    def depth(symbol: str):
        ex = ctx.ex
        return plain(ex.feed.depth(_stock(ex, symbol)))

    @app.get("/api/trades/{symbol}")
    @unlocked(MARKET_DATA)
    def trades(symbol: str, limit: int = Query(50, ge=1, le=200)):
        ex = ctx.ex
        return plain(ex.feed.trades(_stock(ex, symbol), limit))

    @app.get("/api/fundamentals/{symbol}")
    @unlocked(MARKET_DATA)
    def fundamentals(symbol: str):
        ex = ctx.ex
        symbol = _stock(ex, symbol)
        info, q = ex.feed.symbols()[symbol], ex.feed.quote(symbol)
        if q is None or q.last <= 0:
            raise NotFound(f"no quote has arrived for {symbol} yet")
        last = float(q.last)
        daily = ex.feed.bars(symbol, "1d", 252)
        eps, div = info.get("eps", 0), info.get("dividend", 0)
        vols = [b["volume"] for b in daily[-30:]]
        # A live quote has no high or low until the provider's daily bar arrives; use the bars alone then.
        highs = [b["high"] for b in daily] + ([float(q.high)] if q.high is not None else [])
        lows = [b["low"] for b in daily] + ([float(q.low)] if q.low is not None else [])
        return {"symbol": symbol, "name": info["name"], "sector": info["sector"], "type": info.get("type", "stock"),
                "market_cap": last * info.get("shares_outstanding", 0), "shares_outstanding": info.get("shares_outstanding"),
                "eps": eps, "pe": last / eps if eps and eps > 0 else None, "dividend": div,
                "dividend_yield_pct": div / last * 100 if last else None,
                "high_52w": max(highs) if highs else None,
                "low_52w": min(lows) if lows else None,
                "avg_volume_30d": sum(vols) / len(vols) if vols else None,
                "historical_volatility": info.get("vol"), "simulated": ex.feed.simulated}

    @app.get("/api/movers")
    @unlocked(MARKET_DATA)
    def movers():
        ex = ctx.ex
        rows = [r for s in ex.feed.symbols() if (r := quote_row(ex, s)) and "change_pct" in r]
        for r in rows:
            r.update(name=ex.feed.symbols()[r["symbol"]]["name"], sector=ex.feed.symbols()[r["symbol"]]["sector"])
        rows.sort(key=lambda r: r["change_pct"], reverse=True)
        return {"gainers": rows[:5], "losers": rows[::-1][:5],
                "active": sorted(rows, key=lambda r: r["volume"], reverse=True)[:5], "all": rows}

    @app.get("/api/screener")
    @unlocked(MARKET_DATA)
    def screener(sector: str = "", min_price: Optional[float] = None, max_price: Optional[float] = None,
                 min_change_pct: Optional[float] = None, max_change_pct: Optional[float] = None,
                 min_volume: Optional[float] = None, max_pe: Optional[float] = None,
                 min_dividend_yield: Optional[float] = None):
        ex = ctx.ex
        out = []
        for s, info in ex.feed.symbols().items():
            r = quote_row(ex, s)
            if not r or r["last"] <= 0:  # no trade price yet
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
    @unlocked(MARKET_DATA)
    def expirations(symbol: str):
        ex = ctx.ex
        _stock(ex, symbol)
        today = cal.ny_date(ex.now())
        listed = ex.feed.option_expirations(symbol.upper()) or optchain.expirations(today)
        return [{"date": d.isoformat(), "dte": (d - today).days} for d in listed
                if cal.close_ts(d) > ex.now()]

    @app.get("/api/options/chain/{symbol}")
    @unlocked(MARKET_DATA)
    def option_chain(symbol: str, expiry: str):
        ex = ctx.ex
        symbol = _stock(ex, symbol)
        try:
            exp = date.fromisoformat(expiry)
        except ValueError:
            raise ValueError("expiry must be YYYY-MM-DD")
        if exp not in (ex.feed.option_expirations(symbol) or optchain.expirations(cal.ny_date(ex.now()))):
            raise NotFound("expiry is not listed")
        provided = ex.feed.option_chain(symbol, exp, ex.now())
        if provided is not None:
            return plain(provided)
        q = ex.feed.quote(symbol)
        if q is None or q.last <= 0:
            raise NotFound(f"no quote has arrived for {symbol} yet")
        return {"underlying": symbol, "expiry": expiry, "spot": float(q.last), "simulated": True,
                "source": "model", "source_label": "model prices, not market data",
                "rows": plain(optchain.chain(symbol, exp, float(q.last), ex.feed.base_vol(symbol), ex.now(),
                                             ex.feed.event_variance(symbol, cal.close_ts(exp), ex.now())))}

    # ---- accounts ------------------------------------------------------------
    @app.get("/api/accounts")
    def accounts():
        ex = ctx.ex
        return [plain(ex.summary(a)) for a in ex.accounts]

    @app.post("/api/accounts")
    def create_account(body: dict = Body(...)):
        ex = ctx.ex
        from ..core.money import parse as parse_num
        acct = ex.create_account(str(body.get("name") or ""), body.get("type", "margin"),
                                 parse_num(body.get("starting_cash", 100000), "starting cash"))
        return plain(ex.summary(acct.id))

    @app.post("/api/accounts/{aid}/reset")
    def reset_account(aid: str, body: dict = Body(default={})):
        ex = ctx.ex
        from ..core.money import parse as parse_num
        ex.account(aid)
        raw = body.get("starting_cash")
        cash = parse_num(raw, "starting cash") if raw not in (None, "") else None
        ex.reset_account(aid, cash)
        return plain(ex.summary(aid))

    @app.delete("/api/accounts/{aid}")
    def delete_account(aid: str):
        ex = ctx.ex
        ex.delete_account(aid)
        return {"ok": True}

    @app.get("/api/accounts/{aid}")
    def account(aid: str):
        ex = ctx.ex
        return plain(ex.summary(aid))

    @app.get("/api/accounts/{aid}/positions")
    def positions(aid: str):
        ex = ctx.ex
        return plain(ex.positions(aid))

    @app.get("/api/accounts/{aid}/orders")
    def orders(aid: str, status: str = "all"):
        ex = ctx.ex
        return [order_row(o) for o in ex.orders(aid, status)]

    @app.get("/api/accounts/{aid}/fills")
    def fills(aid: str, limit: int = Query(500, ge=1, le=5000)):
        ex = ctx.ex
        rows = [dict(plain(f), description=describe(f.symbol)) for f in ex.account(aid).fills[-limit:]]
        return rows[::-1]

    @app.get("/api/accounts/{aid}/ledger")
    def ledger(aid: str, limit: int = Query(500, ge=1, le=5000)):
        ex = ctx.ex
        txns = ex.account(aid).ledger.transactions[-limit:]
        return [{"id": t.id, "ts": t.ts, "kind": t.kind, "memo": t.memo,
                 "postings": [[a, float(v)] for a, v in t.postings]} for t in txns][::-1]

    @app.post("/api/accounts/{aid}/orders/preview")
    def preview(aid: str, body: dict = Body(...)):
        ex = ctx.ex
        return plain(ex.preview(aid, body))

    @app.post("/api/accounts/{aid}/orders")
    def submit(aid: str, body: dict = Body(...)):
        ex = ctx.ex
        return [order_row(o) for o in ex.submit(aid, body)]

    @app.delete("/api/accounts/{aid}/orders")
    def cancel_all(aid: str):
        ex = ctx.ex
        ex.account(aid)
        return {"canceled": ex.cancel_all(aid)}

    @app.delete("/api/accounts/{aid}/orders/{oid}")
    def cancel(aid: str, oid: str):
        ex = ctx.ex
        return order_row(ex.cancel(aid, oid))

    @app.patch("/api/accounts/{aid}/orders/{oid}")
    def replace(aid: str, oid: str, body: dict = Body(...)):
        ex = ctx.ex
        return order_row(ex.replace(aid, oid, body))

    # ---- watchlists and alerts ---------------------------------------------------
    @app.get("/api/watchlists")
    def watchlists():
        ex = ctx.ex
        return ex.watchlists

    @app.put("/api/watchlists/{name}")
    def put_watchlist(name: str, body: list = Body(...)):
        ex = ctx.ex
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
        ex = ctx.ex
        ex.watchlists.pop(name, None)
        ex.dirty = True
        return ex.watchlists

    @app.get("/api/alerts")
    def alerts():
        ex = ctx.ex
        return ex.alerts

    @app.post("/api/alerts")
    def add_alert(body: dict = Body(...)):
        ex = ctx.ex
        return ex.add_alert(str(body.get("symbol", "")), body.get("op"), body.get("price"))

    @app.delete("/api/alerts/{alert_id}")
    def remove_alert(alert_id: str):
        ex = ctx.ex
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
                    if "account" in msg:  # an id string; anything else would break the broadcast for everyone
                        clients[ws]["account"] = msg["account"] if isinstance(msg["account"], str) else None
        except (WebSocketDisconnect, Exception):
            clients.pop(ws, None)

    # ---- extension routes ----------------------------------------------------------
    # Every module named app/api/routes_*.py that defines register(app, ctx) is loaded here,
    # so new features add routes without editing this file. ctx.ex is the active exchange
    # (live, or the replay sandbox); look it up per request.
    import importlib
    import pkgutil
    pkg = importlib.import_module("app.api")
    for mod in sorted(m.name for m in pkgutil.iter_modules(pkg.__path__) if m.name.startswith("routes_")):
        importlib.import_module(f"app.api.{mod}").register(app, ctx)

    # ---- static UI -----------------------------------------------------------------
    if os.path.isdir(dist):
        app.mount("/assets", StaticFiles(directory=os.path.join(dist, "assets")), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def ui(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(os.path.join(dist, "index.html"))

    return app


def app_factory() -> FastAPI:
    from ..core import envfile
    envfile.load_settings()  # before anything reads a setting (data folder, seed, feed, access, UI folder)
    return create_app()
