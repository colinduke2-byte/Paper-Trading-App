"""Market discovery routes: named watchlists with layouts, symbol search, screener, market overview, compare.

The older /api/watchlists routes (in server.py) keep working; the lists they edit are the same
Exchange.watchlists. Layouts, saved screens and recent symbols are kept in discovery.json in the
live data directory (app/discovery/store.py). Every row says where its quote came from.
"""
from typing import Optional

from fastapi import Body, Query

from ..discovery import data as dd
from ..discovery import screener as sc
from ..discovery import stats
from ..discovery.search import MAX_LIMIT as SEARCH_MAX, SearchIndex
from ..discovery.store import (COLUMNS, MAX_LISTS, MAX_RECENT, MAX_SYMBOLS, DiscoveryStore, check_layout,
                               check_name)


SHOWN_SECONDS = 120  # the watchlist panel asks for rows every 15 s while it is open


def register(app, ctx):
    store = DiscoveryStore(ctx.live.data_dir)
    index = SearchIndex()
    services: dict = {}  # id(feed) -> (feed, FeedData); the replay sandbox has its own feed

    def svc() -> dd.FeedData:
        feed = ctx.ex.feed
        hit = services.get(id(feed))
        if hit is None or hit[0] is not feed:
            if len(services) > 4:
                services.clear()
            hit = services[id(feed)] = (feed, dd.FeedData(feed, ctx.ex.now))
        return hit[1]

    # Lists a browser asked rows for recently: the live feed streams these first and does not poll
    # hidden lists while any list is being shown (see marketdata.config.wanted_symbols).
    shown: dict = {}  # list name -> exchange time of the last rows request

    def watchlists_shown() -> list:
        now = ctx.live.now()
        recent = [(t, n) for n, t in list(shown.items()) if now - t < SHOWN_SECONDS]
        return [n for _, n in sorted(recent, reverse=True)]

    ctx.live.watchlists_shown = watchlists_shown
    app.state.discovery = {"store": store, "service": svc, "shown": shown}  # tests read these

    def stock(symbol: str) -> str:
        s = str(symbol or "").upper().strip()
        if not ctx.ex.is_stock(s):
            raise KeyError(f"unknown symbol {s or '(empty)'}")
        return s

    # ---- watchlists ---------------------------------------------------------------------------------
    def find(name: str) -> str:
        ex = ctx.ex
        if name in ex.watchlists:
            return name
        for k in ex.watchlists:
            if k.lower() == name.lower():
                return k
        raise KeyError(f"no watchlist called {name}")

    def taken(name: str, besides: Optional[str] = None) -> bool:
        return any(k.lower() == name.lower() and k != besides for k in ctx.ex.watchlists)

    def clean_symbols(symbols) -> tuple:
        if not isinstance(symbols, list):
            raise ValueError("symbols must be a list")
        out, unknown = [], []
        for s in symbols[:1000]:
            s = str(s).upper().strip()
            if not s or s in out:
                continue
            (out if ctx.ex.is_stock(s) else unknown).append(s)
        return out, unknown

    def list_view(name: str) -> dict:
        return {"name": name, "symbols": list(ctx.ex.watchlists.get(name, [])), "layout": store.layout(name)}

    def all_lists() -> dict:
        ex = ctx.ex
        if not ex.watchlists:
            with ctx.lock:
                ex.watchlists["Main"] = []
                ex.dirty = True
        return {"lists": [list_view(n) for n in ex.watchlists],
                "columns": [{"key": k, "label": v} for k, v in COLUMNS.items()],
                "limits": {"lists": MAX_LISTS, "symbols": MAX_SYMBOLS}}

    def rebuild(pairs: list) -> None:
        """Replace the lists in place, keeping the same dict object (other code holds it)."""
        ex = ctx.ex
        ex.watchlists.clear()
        ex.watchlists.update(pairs)
        ex.dirty = True

    @app.get("/api/watchlist-sets")
    def get_sets():
        return all_lists()

    @app.post("/api/watchlist-sets")
    def create_set(body: dict = Body(...)):
        ex = ctx.ex
        name = check_name(body.get("name"))
        with ctx.lock:
            if taken(name):
                raise ValueError(f"a watchlist called {name} already exists")
            if len(ex.watchlists) >= MAX_LISTS:
                raise ValueError(f"at most {MAX_LISTS} watchlists")
            syms, _ = clean_symbols(body.get("symbols") or [])
            ex.watchlists[name] = syms[:MAX_SYMBOLS]
            ex.dirty = True
        store.set_layout(name, check_layout(body.get("layout") or {}))
        return all_lists()

    @app.post("/api/watchlist-sets/{name}/duplicate")
    def duplicate_set(name: str, body: dict = Body(default={})):
        ex = ctx.ex
        src = find(name)
        new = check_name(body.get("name") or f"{src} copy"[:30])
        with ctx.lock:
            if taken(new):
                raise ValueError(f"a watchlist called {new} already exists")
            if len(ex.watchlists) >= MAX_LISTS:
                raise ValueError(f"at most {MAX_LISTS} watchlists")
            pairs = []
            for k, v in ex.watchlists.items():
                pairs.append((k, v))
                if k == src:
                    pairs.append((new, list(v)))
            rebuild(pairs)
        store.set_layout(new, store.layout(src))
        return all_lists()

    @app.patch("/api/watchlist-sets/{name}")
    def update_set(name: str, body: dict = Body(...)):
        """Rename ({"name"}), replace the symbols in order ({"symbols"}) and/or change the layout."""
        ex = ctx.ex
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        unknown = set(body) - {"name", "symbols", "columns", "density", "sort"}
        if unknown:
            raise ValueError(f"unknown field {sorted(unknown)[0]}")
        cur = find(name)
        layout_part = {k: body[k] for k in ("columns", "density", "sort") if k in body}
        layout = check_layout(layout_part, store.layout(cur)) if layout_part else None
        new = check_name(body["name"]) if "name" in body else cur
        syms = None
        if "symbols" in body:
            syms, bad = clean_symbols(body["symbols"])
            if len(syms) > MAX_SYMBOLS:
                raise ValueError(f"a watchlist holds at most {MAX_SYMBOLS} symbols")
        with ctx.lock:
            if new != cur and taken(new, besides=cur):
                raise ValueError(f"a watchlist called {new} already exists")
            pairs = [((new if k == cur else k), (syms if k == cur and syms is not None else v))
                     for k, v in ex.watchlists.items()]
            rebuild(pairs)
        if new != cur:
            store.rename(cur, new)
            if cur in shown:
                shown[new] = shown.pop(cur)
        if layout is not None:
            store.set_layout(new, layout)
        return all_lists()

    @app.delete("/api/watchlist-sets/{name}")
    def delete_set(name: str):
        ex = ctx.ex
        cur = find(name)
        with ctx.lock:
            if len(ex.watchlists) <= 1:
                raise ValueError("keep at least one watchlist; empty it instead")
            ex.watchlists.pop(cur)
            ex.dirty = True
        store.forget(cur)
        return all_lists()

    @app.put("/api/watchlist-order")
    def order_sets(body: list = Body(...)):
        ex = ctx.ex
        names = [find(str(n)) for n in body]
        if sorted(names) != sorted(ex.watchlists) or len(set(names)) != len(names):
            raise ValueError("the order must name every watchlist exactly once")
        with ctx.lock:
            rebuild([(n, ex.watchlists[n]) for n in names])
        return all_lists()

    @app.post("/api/watchlist-sets/{name}/symbols")
    def add_symbols(name: str, body: dict = Body(...)):
        """Add symbols at the end (or the start with "at": "start"); unknown and duplicates are skipped."""
        ex = ctx.ex
        cur = find(name)
        syms, unknown = clean_symbols(body.get("symbols") or [])
        with ctx.lock:
            have = list(ex.watchlists[cur])
            new = [s for s in syms if s not in have]
            room = max(0, MAX_SYMBOLS - len(have))
            added, full = new[:room], new[room:]
            ex.watchlists[cur] = added + have if body.get("at") == "start" else have + added
            ex.dirty = True
        return {"added": added, "already": [s for s in syms if s in have], "unknown": unknown, "full": full,
                "list": list_view(cur)}

    @app.get("/api/watchlist-sets/{name}/rows")
    def list_rows(name: str, columns: str = "", account: str = ""):
        ex = ctx.ex
        cur = find(name)
        if ex is ctx.live:
            shown[cur] = ex.now()
        cols = [c for c in columns.split(",") if c] or store.layout(cur)["columns"]
        bad = [c for c in cols if c not in COLUMNS]
        if bad:
            raise ValueError(f"unknown column {bad[0]}")
        d = svc()
        pos = {}
        if account and account in ex.accounts:
            for p in ex.positions(account):
                if p["kind"] == "stock":
                    pos[p["symbol"]] = p
        want_hist, want_spark = "range_52w" in cols, "sparkline" in cols
        rows = []
        for s in list(ex.watchlists[cur]):
            r = d.row(s, history=want_hist)
            p = pos.get(s)
            r["position_qty"] = p["qty"] if p else None
            r["unrealized_pnl"] = p["unrealized_pnl"] if p else None
            if want_spark:
                bars = d.intraday(s)
                r["sparkline"] = stats.sparkline(bars) if bars else None
            rows.append(r)
        return ctx.plain({"name": cur, "rows": rows, "columns": cols, "remote": d.remote,
                          "note": remote_note(d) if d.remote and (want_hist or want_spark) else None})

    def remote_note(d) -> str:
        return (f"52-week ranges and intraday lines use daily and 5-minute history fetched for at most "
                f"{dd.HISTORY_PER_MINUTE} symbols a minute; a dash means it has not been fetched yet.")

    # ---- symbol search --------------------------------------------------------------------------------
    @app.get("/api/search")
    def search(q: str = Query("", max_length=60), limit: int = Query(10, ge=1, le=SEARCH_MAX), types: str = ""):
        kinds = {t for t in types.split(",") if t}
        if kinds - {"stock", "etf"}:
            raise ValueError("types must be stock and/or etf")
        return index.search(ctx.ex.feed.symbols(), q, limit, kinds or None)

    @app.get("/api/search/recent")
    def recent():
        info = ctx.ex.feed.symbols()
        return [{"symbol": s, "name": info[s].get("name") or s, "type": info[s].get("type") or "stock"}
                for s in store.recent if s in info][:MAX_RECENT]

    @app.post("/api/search/recent")
    def add_recent(body: dict = Body(...)):
        store.touch_recent(stock(body.get("symbol")))
        return recent()

    # ---- screener ----------------------------------------------------------------------------------
    def universe_meta(d: dd.FeedData) -> dict:
        n = len(ctx.ex.feed.symbols())
        if not d.remote:
            kind = "simulated" if ctx.ex.feed.simulated else "recorded"
            label = f"All {n} {kind} symbols" + (" in the replay" if ctx.mode == "replay" else "")
            return {"label": label, "total": n, "with_quotes": n, "with_history": n, "complete": True,
                    "remote": False, "fetched_from": None, "fetched_to": None, "error": None,
                    "note": "Every symbol is screened live from the current prices."
                            + (" The companies are fictional." if ctx.ex.feed.simulated else "")}
        scanned = d.scanned()
        times = [v[0] for v in scanned.values()]
        minutes = max(1, round(n / dd.SCAN_BATCH / dd.SCAN_PER_MINUTE))
        return {"label": f"US-listed stocks and funds on Alpaca ({n:,} symbols)", "total": n,
                "with_quotes": len(scanned), "with_history": sum(1 for v in list(d._hist.values()) if v[1]),
                "complete": len(d._scan) >= n, "remote": True,
                "fetched_from": min(times) if times else None, "fetched_to": max(times) if times else None,
                "error": d.scan_error,
                "note": (f"Prices come from Alpaca snapshots fetched {dd.SCAN_BATCH} symbols per request, at most "
                         f"{dd.SCAN_PER_MINUTE} requests a minute, so one pass over every symbol takes about "
                         f"{minutes} minutes while discovery pages are open. Filters on 52-week range, moving "
                         f"averages, RSI and relative volume only cover symbols whose daily history has been "
                         f"fetched (at most {dd.HISTORY_PER_MINUTE} a minute, kept for 6 hours). Alpaca does not "
                         f"provide sector, market cap, P/E, dividend yield or option availability.")}

    def universe_rows(d: dd.FeedData, history: bool) -> list:
        if not d.remote:
            return [d.row(s, history=history) for s in d.universe()]
        d.scan()
        syms = set(d.scanned()) | {s for s in ctx.ex.feed.quotes if s in ctx.ex.feed.symbols()}
        return [d.row(s, history=history, fetch=False) for s in sorted(syms)]

    @app.get("/api/screener/fields")
    def screener_fields():
        d = svc()
        sectors = sorted({i.get("sector") for i in ctx.ex.feed.symbols().values() if i.get("sector")})
        avail = lambda k: not (d.remote and k in sc.FUNDAMENTAL)
        fields = [{"key": k, "label": v[0], "unit": v[1], "history": v[2], "kind": "number", "available": avail(k)}
                  for k, v in sc.NUMERIC.items()]
        fields += [{"key": "type", "label": "Asset type", "kind": "choice", "choices": ["stock", "etf"], "available": True},
                   {"key": "sector", "label": "Sector", "kind": "choice", "choices": sectors, "available": avail("sector")},
                   {"key": "has_options", "label": "Has options", "kind": "bool", "available": avail("has_options")}]
        return {"fields": fields, "presets": sc.PRESETS, "universe": universe_meta(d)}

    @app.post("/api/screener/run")
    def screener_run(body: dict = Body(...)):
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        filters = sc.check_filters(body.get("filters"))
        sort, desc = body.get("sort") or None, bool(body.get("desc", True))
        if sort is not None and sort not in sc.SORTABLE:
            raise ValueError(f"cannot sort by {sort}")
        limit = body.get("limit", 100)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= sc.MAX_LIMIT:
            raise ValueError(f"limit must be a whole number from 1 to {sc.MAX_LIMIT}")
        d = svc()
        hist = sc.needs_history(filters) or (sort in sc.NUMERIC and sc.NUMERIC[sort][2])
        rows = universe_rows(d, history=True)
        pending = 0
        if d.remote and hist:
            # Fetch daily history for the most traded rows that pass the other filters, within the budget.
            quick = [f for f in filters if not (f["field"] in sc.NUMERIC and sc.NUMERIC[f["field"]][2])]
            need = [r for r in sc.evaluate(rows, quick) if not r.get("history")]
            need.sort(key=lambda r: -(r.get("dollar_volume") or 0))
            fresh = {}
            for r in need:
                if d.history_budget.used() >= d.history_budget.per_minute:
                    break
                fresh[r["symbol"]] = d.row(r["symbol"], history=True, fetch=True)
            rows = [fresh.get(r["symbol"], r) for r in rows]
            pending = sum(1 for r in sc.evaluate(rows, quick) if not r.get("history"))
        found = sc.sort_rows(sc.evaluate(rows, filters), sort, desc)
        return ctx.plain({"rows": found[:limit], "matched": len(found), "screened": len(rows),
                          "history_pending": pending, "universe": universe_meta(d), "as_of": d.clock()})

    @app.get("/api/screens")
    def screens():
        return {"saved": store.screens, "presets": sc.PRESETS}

    @app.post("/api/screens")
    def save_screen(body: dict = Body(...)):
        return store.add_screen(sc.check_screen(body))

    @app.put("/api/screens/{sid}")
    def update_screen(sid: str, body: dict = Body(...)):
        return store.update_screen(sid, sc.check_screen(body))

    @app.delete("/api/screens/{sid}")
    def delete_screen(sid: str):
        store.delete_screen(sid)
        return {"ok": True}

    # ---- market overview ----------------------------------------------------------------------------
    @app.get("/api/markets/overview")
    def overview(limit: int = Query(10, ge=1, le=25)):
        d = svc()
        rows = [r for r in universe_rows(d, history=True) if r["last"] is not None]
        movers = [r for r in rows if r["change_pct"] is not None and r["type"] != "etf"]
        floor = None
        if d.remote:  # leave out thinly traded and sub-dollar listings, whose moves are mostly noise
            floor = {"min_price": 1, "min_volume": 100_000}
            movers = [r for r in movers if r["last"] >= 1 and (r["volume"] or 0) >= 100_000]
        gain = sorted(movers, key=lambda r: (-r["change_pct"], r["symbol"]))
        lose = sorted(movers, key=lambda r: (r["change_pct"], r["symbol"]))
        active = sorted([r for r in rows if r["volume"] is not None], key=lambda r: (-(r["dollar_volume"] or 0), r["symbol"]))
        slim = lambda r: {k: r.get(k) for k in ("symbol", "name", "type", "sector", "last", "change", "change_pct",
                                                 "volume", "dollar_volume", "market_cap", "quote_source")}
        idx_syms = dd.INDEX_SYMBOLS_REMOTE if d.remote else [r["symbol"] for r in rows if r["type"] == "etf"]
        by = {r["symbol"]: r for r in rows}
        indices = []
        for s in idx_syms:
            if s in by:
                t = slim(by[s])
                bars = d.intraday(s)
                t["sparkline"] = stats.sparkline(bars) if bars else None
                indices.append(t)
        ch = [r["change_pct"] for r in rows if r["change_pct"] is not None]
        hist = [r for r in rows if r.get("history") and r.get("sma50") is not None]
        breadth = {"advancers": sum(1 for c in ch if c > 0), "decliners": sum(1 for c in ch if c < 0),
                   "unchanged": sum(1 for c in ch if c == 0), "total": len(ch),
                   "up_volume": sum(r["volume"] or 0 for r in rows if (r["change_pct"] or 0) > 0),
                   "down_volume": sum(r["volume"] or 0 for r in rows if (r["change_pct"] or 0) < 0),
                   "new_highs": sum(1 for r in rows if r.get("pct_from_high_52w") is not None and r["pct_from_high_52w"] <= 0),
                   "new_lows": sum(1 for r in rows if r.get("pct_from_low_52w") is not None and r["pct_from_low_52w"] <= 0),
                   "above_sma50": sum(1 for r in hist if r["last"] > r["sma50"]), "with_history": len(hist)}
        sectors, sizing = [], "market_cap"
        if d.remote:
            sizing = "dollar_volume"
            for s, label in dd.SECTOR_ETFS.items():
                if s in by and by[s]["change_pct"] is not None:
                    r = by[s]
                    sectors.append({"sector": label, "change_pct": r["change_pct"], "size": r["dollar_volume"] or 0,
                                    "count": 1, "members": [dict(slim(r), size=r["dollar_volume"] or 0)]})
        else:
            groups: dict = {}
            for r in rows:
                if r["type"] == "etf" or not r["sector"] or r["change_pct"] is None:
                    continue
                groups.setdefault(r["sector"], []).append(r)
            for name, members in groups.items():
                size = sum(m["market_cap"] or 0 for m in members)
                w = sum((m["market_cap"] or 0) * m["change_pct"] for m in members)
                sectors.append({"sector": name, "change_pct": w / size if size else sum(m["change_pct"] for m in members) / len(members),
                                "size": size, "count": len(members),
                                "members": sorted([dict(slim(m), size=m["market_cap"] or 0) for m in members],
                                                  key=lambda m: -m["size"])})
        sectors.sort(key=lambda s: -s["size"])
        return ctx.plain({"gainers": [slim(r) for r in gain[:limit]], "losers": [slim(r) for r in lose[:limit]],
                          "active": [slim(r) for r in active[:limit]], "indices": indices, "breadth": breadth,
                          "sectors": sectors, "sizing": sizing, "mover_floor": floor,
                          "sector_note": ("Sectors are shown through the SPDR sector funds, sized by today's dollar "
                                          "volume: Alpaca provides no sector or market cap data.") if d.remote else
                                         "Tiles are sized by market cap and coloured by today's change.",
                          "universe": universe_meta(d), "as_of": d.clock(), "simulated": ctx.ex.feed.simulated})

    # ---- compare -------------------------------------------------------------------------------------
    @app.get("/api/compare")
    def compare(symbols: str = Query(..., max_length=100)):
        syms = list(dict.fromkeys(s.strip().upper() for s in symbols.split(",") if s.strip()))
        if not 2 <= len(syms) <= 5:
            raise ValueError("compare 2 to 5 different symbols")
        syms = [stock(s) for s in syms]
        d = svc()
        out = []
        for s in syms:
            r = d.row(s, history=True, fetch=True)
            daily = d.daily(s) or []
            q = ctx.ex.feed.quote(s)
            hist, _ = stats.split_today(daily, d.today(q.ts if q else None))
            r["performance"] = stats.performance(hist, r["last"], d.today(q.ts if q else None), r["prev_close"])
            out.append({k: r.get(k) for k in (
                "symbol", "name", "type", "sector", "last", "change", "change_pct", "performance", "market_cap", "pe",
                "dividend_yield", "high_52w", "low_52w", "range_52w_pos", "volatility", "avg_volume_30d", "rsi14",
                "pct_vs_sma50", "pct_vs_sma200", "history_days", "quote_source")})
        return ctx.plain({"symbols": out, "periods": list(stats.PERIODS), "as_of": d.clock(), "simulated": ctx.ex.feed.simulated,
                          "note": "Performance is the change in price from the close on or before each date; dividends are not included."})
