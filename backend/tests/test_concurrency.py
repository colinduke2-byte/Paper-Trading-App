"""The exchange lock and what it guarantees (docs/CONCURRENCY.md): the lock itself, which routes run
under it, bounded waits, the tick's priority, atomic order changes and ordered state files.
The many-thread stress tests are in test_concurrency_stress.py."""
import json
import os
import threading
import time

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from fastapi.testclient import TestClient

from app.api import locking as api_locking
from app.api.server import create_app
from app.core.clock import SimClock
from app.core.locking import Busy, StateLock, held
from app.exchange import Exchange
from app.marketdata.replay.control import ReplayController
from app.marketdata.replay.library import BUNDLED_DIR
from app.marketdata.synthetic import SyntheticFeed
from app.options import chain
from app.options.symbology import build
from app.core import calendar as cal
from helpers import D, T0, make_exchange, move
from stress import problems

SESSION = sorted(f for f in os.listdir(BUNDLED_DIR) if f.endswith(".gz"))[0]


def make_app(tmp=None, run_loop=False, **kw):
    ex = Exchange(SyntheticFeed(seed=3, now=T0), SimClock(T0), data_dir=str(tmp) if tmp else None)
    ex.create_account("Margin", "margin", D("1000000"))
    ex.tick()
    app = create_app(ex, run_loop=run_loop, **kw)
    return TestClient(app), app, ex


def in_thread(fn):
    out = {}

    def go():
        try:
            out["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - handed to the caller
            out["error"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, out


class Holder:
    """Holds a lock on another thread until released: a stuck request."""

    def __init__(self, lock, label="stuck request"):
        self.lock, self.label = lock, label
        self.taken, self.done = threading.Event(), threading.Event()

    def __enter__(self):
        def hold():
            with self.lock.hold(5, label=self.label):
                self.taken.set()
                self.done.wait(30)
        self.thread = threading.Thread(target=hold, daemon=True)
        self.thread.start()
        assert self.taken.wait(5)
        return self

    def __exit__(self, *exc):
        self.done.set()
        self.thread.join(5)


# ---- the lock --------------------------------------------------------------------------------------
def test_lock_is_reentrant_bounded_and_knows_its_holder():
    lock = StateLock()
    assert not held() and not lock.owned() and lock.holder() is None
    with lock:
        with lock.hold(0, label="inner"):
            assert lock.owned() and held()
        assert lock.owned()
        t, out = in_thread(lambda: lock.acquire(0.05))
        t.join(5)
        assert out["value"] is False  # another thread gives up after its timeout
        t, out = in_thread(lambda: lock.hold(0.01).__enter__())
        t.join(5)
        assert isinstance(out["error"], Busy) and "busy" in str(out["error"])
    assert not lock.owned() and not held() and lock.longest[0] > 0
    with pytest.raises(RuntimeError):
        lock.release()
    t, out = in_thread(lambda: lock.acquire(1, label="worker") and lock.holder()[0])
    t.join(5)
    assert out["value"] == "worker"  # (and it never released: the lock stays taken)
    assert lock.acquire(0) is False and lock.holder()[0] == "worker"


def test_urgent_waiter_goes_first_and_the_others_keep_their_order():
    lock, order = StateLock(), []
    lock.acquire()
    threads = []

    def waiter(name, urgent):
        def go():
            lock.acquire(10, urgent=urgent)
            order.append(name)
            lock.release()
        return go

    for name in ("a", "b", "c"):
        threads.append(threading.Thread(target=waiter(name, False), daemon=True))
        threads[-1].start()
        deadline = time.monotonic() + 5
        while len(lock._queue) < len(threads) and time.monotonic() < deadline:
            time.sleep(0.001)
    threads.append(threading.Thread(target=waiter("tick", True), daemon=True))
    threads[-1].start()
    deadline = time.monotonic() + 5
    while not lock._urgent and time.monotonic() < deadline:
        time.sleep(0.001)
    lock.release()
    for t in threads:
        t.join(5)
    assert order == ["tick", "a", "b", "c"]


def test_a_waiter_that_gives_up_does_not_block_the_queue():
    lock = StateLock()
    lock.acquire()
    t, out = in_thread(lambda: lock.acquire(0.02))
    t.join(5)
    assert out["value"] is False and not lock._queue
    lock.release()
    t, out = in_thread(lambda: lock.acquire(1))
    t.join(5)
    assert out["value"] is True


# ---- which routes run under the lock ------------------------------------------------------------------
# Routes that do not run under the lock, each for a stated reason. A new route is locked by default;
# adding one here is a decision to review (see docs/CONCURRENCY.md, "Adding a route").
UNLOCKED_READS = {
    "/api/symbols", "/api/quotes", "/api/bars/{symbol:path}", "/api/depth/{symbol:path}", "/api/trades/{symbol:path}",
    "/api/fundamentals/{symbol:path}", "/api/movers", "/api/screener", "/api/options/expirations/{symbol}",
    "/api/options/chain/{symbol}",  # market data: the feed only, and a live feed asks the provider
    "/api/accounts/{aid}/analytics", "/api/accounts/{aid}/statements/{month}",
    "/api/accounts/{aid}/statements/{month}/html",  # fetch price history, then read the account under ctx.locked()
    "/api/replay/sessions",  # reads session files
    "/api/auth/state", "/api/research/sources", "/api/research/news/{symbol}", "/api/research/calendar/{symbol}",
    "/api/research/news", "/api/research/economic-calendar", "/api/research/earnings-calendar",
    "/api/research/statements/{symbol}",  # simulated research: exempt by path like the other research routes
    "/api/research/corporate-actions/{symbol}", "/api/research/fundamentals/{symbol}",
    "/api/research/news", "/api/research/economic-calendar", "/api/research/earnings-calendar",
    "/api/research/statements/{symbol}",
    "/api/chartstate", "/api/chartstate/drawings/{sym:path}",  # chart settings: their own file and lock, no exchange state
    # backups (PR 19): file I/O must not run under the lock; they take ctx.locked() only to serialise
    "/api/data/status", "/api/data/backups", "/api/data/backups/{name}", "/api/data/export",
    "/api/data/accounts/{aid}/export", "/api/data/recovery",
    # discovery (PR 20): may ask the provider, or reads its own file; exchange state under a short ctx.locked()
    "/api/watchlist-sets/{name}/rows", "/api/search", "/api/search/recent", "/api/screener/fields", "/api/screens",
    "/api/markets/overview", "/api/compare",
}
SELF_LOCKING_WRITES = {  # take ctx.lock themselves, in pieces: proven by test_every_changing_route_answers_busy...
    ("POST", "/api/replay/start"), ("POST", "/api/replay/step"), ("POST", "/api/replay/seek"), ("POST", "/api/replay/exit"),
    # backups and restore: serialise or replace state under ctx.locked() in DataService, write files outside it.
    # Proven by test_data_routes_answer_busy_and_never_block_the_event_loop_while_the_lock_is_stuck.
    ("POST", "/api/accounts/{aid}/reset"), ("DELETE", "/api/accounts/{aid}"),  # backup outside the lock, then the action
    ("POST", "/api/data/backups"), ("DELETE", "/api/data/backups/{name}"), ("POST", "/api/data/restore/preview"),
    ("POST", "/api/data/restore/apply"), ("POST", "/api/data/accounts/import/preview"),
    ("POST", "/api/data/accounts/import/apply"), ("POST", "/api/data/recovery/restore"),
    ("POST", "/api/data/recovery/fresh"), ("POST", "/api/data/recovery/quit"),
}
NO_EXCHANGE_STATE = {  # the session store and the chart-state file have their own locks
    ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"),
    ("POST", "/api/search/recent"), ("POST", "/api/screener/run"), ("POST", "/api/screens"),  # discovery's own store
    ("PUT", "/api/screens/{sid}"), ("DELETE", "/api/screens/{sid}"),
    ("PUT", "/api/chartstate/settings"), ("PUT", "/api/chartstate/drawings/{sym:path}"), ("DELETE", "/api/chartstate"),
}


def api_routes(app):
    for r in app.routes:
        path = getattr(r, "path", "")
        if path.startswith("/api/") and hasattr(r, "methods"):
            for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
                yield method, path, r


def test_every_api_route_runs_under_the_lock_unless_listed_here():
    _, app, _ = make_app()
    seen = 0
    for method, path, route in api_routes(app):
        seen += 1
        policy = getattr(route, "lock_policy", None)
        assert policy in ("locked", "unlocked"), f"{method} {path} was not registered through the locking route class"
        if method == "GET":
            assert policy == "locked" or path in UNLOCKED_READS, f"GET {path} reads without the exchange lock"
        else:
            assert policy == "locked" or (method, path) in SELF_LOCKING_WRITES | NO_EXCHANGE_STATE, \
                f"{method} {path} changes state without the exchange lock"
        if policy == "unlocked":
            assert route.lock_reason, f"{method} {path} does not say why it is unlocked"
    assert seen > 60
    locked_writes = [p for m, p, r in api_routes(app) if m != "GET" and r.lock_policy == "locked"]
    assert len(locked_writes) >= 20


def fill_path(path: str, aid: str) -> str:
    for name, value in (("{aid}", aid), ("{symbol}", "MKTX"), ("{oid}", "O00000001"), ("{month}", "2026-11"),
                        ("{kind}", "fills"), ("{jid}", "J1"), ("{alert_id}", "AL00001"), ("{name}", "Main"), ("{sid}", "S1"),
                        ("{sym:path}", "MKTX"), ("{symbol:path}", "MKTX"), ("{sym}", "MKTX")):
        path = path.replace(name, value)
    assert "{" not in path, f"give fill_path a value for {path}"
    return path


def test_every_changing_route_answers_busy_while_the_lock_is_stuck(monkeypatch, tmp_path):
    monkeypatch.setattr(api_locking, "ROUTE_WAIT", 0.02)
    monkeypatch.setattr("app.marketdata.replay.control.COMMAND_WAIT", 0.02)
    c, app, ex = make_app(tmp_path)
    ctx = app.state.ctx
    aid = list(ex.accounts)[0]
    assert c.post("/api/replay/start", json={"session": SESSION.split(".")[0]}).status_code == 200  # so replay routes have work
    sandbox = list(ctx.ex.accounts)[0]
    before = json.dumps([a.to_plain() for a in ctx.ex.accounts.values()], sort_keys=True)
    checked = 0
    with Holder(ctx.lock):
        for method, path, route in api_routes(app):
            if method == "GET" or (method, path) in NO_EXCHANGE_STATE:
                continue
            url = fill_path(path, sandbox)
            body = {"session": SESSION.split(".")[0], "offset": 600, "seconds": 5}
            r = c.request(method, url, json=[] if path.startswith(("/api/watchlists", "/api/watchlist-order")) else body,
                          params={"account_id": sandbox})
            if path == "/api/replay/start":  # refused before it needs the lock; checked without a replay below
                assert r.status_code == 409
                continue
            if path.startswith("/api/data/"):  # refused during a replay; checked without one in the data test
                assert r.status_code in (404, 409, 503), f"{method} {path} answered {r.status_code}"
                continue
            assert r.status_code == 503 and r.json()["busy"] is True and r.headers["retry-after"] == "1", \
                f"{method} {path} answered {r.status_code} {r.text[:200]} while the lock was held"
            assert "busy" in r.json()["detail"]
            checked += 1
        # reads that need the lock say busy too; market data and sign-in state still answer
        assert c.get(f"/api/accounts/{sandbox}").status_code == 503
        assert c.get("/api/status").status_code == 503
        assert c.get("/api/auth/state").status_code == 200
        assert c.get("/api/symbols").status_code == 200
    assert checked >= 23
    assert json.dumps([a.to_plain() for a in ctx.ex.accounts.values()], sort_keys=True) == before
    assert c.get(f"/api/accounts/{sandbox}").status_code == 200  # and everything works again afterwards
    assert c.post("/api/replay/exit").status_code == 200 and aid in ctx.ex.accounts
    with Holder(ctx.lock):
        assert c.post("/api/replay/start", json={"session": SESSION.split(".")[0]}).status_code == 503
    assert ctx.mode == "live"


def test_a_route_added_later_is_locked_by_default_and_sees_one_exchange_across_a_mode_switch(tmp_path):
    c, app, ex = make_app(tmp_path)
    ctx = app.state.ctx
    inside, go, seen = threading.Event(), threading.Event(), []

    @app.post("/api/test/slow")  # a new feature route: nothing special is needed to be safe
    def slow():
        seen.append(ctx.ex)
        inside.set()
        go.wait(10)
        seen.append(ctx.ex)
        return {"mode": ctx.mode, "list": ctx.ex.watchlists.setdefault("Shared", ["MKTX"])}

    assert app.routes[-1].lock_policy == "locked"
    t, out = in_thread(lambda: c.post("/api/test/slow"))
    assert inside.wait(5)
    s, started = in_thread(lambda: c.post("/api/replay/start", json={"session": SESSION.split(".")[0]}))
    s.join(0.3)
    assert s.is_alive() and ctx.mode == "live", "the mode switched while a request was using the exchange"
    go.set()
    t.join(5), s.join(10)
    assert started["value"].status_code == 200 and ctx.mode == "replay"
    assert seen[0] is seen[1] is ex and out["value"].json()["mode"] == "live"
    # the result was copied before the lock was released: it does not alias live state
    ex.watchlists["Shared"].append("TECX")
    assert out["value"].json()["list"] == ["MKTX"]
    assert api_locking.detach({"a": [1, (2, {"b": []})]}) == {"a": [1, (2, {"b": []})]}


def test_an_async_api_route_must_say_how_it_handles_the_lock():
    _, app, _ = make_app()
    with pytest.raises(TypeError, match="async API route"):
        @app.get("/api/test/async")
        async def bad():
            return {}

    @app.get("/api/test/async-ok")
    @api_locking.unlocked("does not touch exchange state")
    async def fine():
        return {}
    assert app.routes[-1].lock_policy == "unlocked"
    with pytest.raises(ValueError):
        api_locking.unlocked("")


# Bodies for the changing routes in the walk below; every other one gets {}. Each of these must answer 200,
# so a route that reaches the provider only past validation is really exercised.
COVERED_CALL = {"qty": 1, "legs": [{"symbol": "AAPL", "side": "buy", "ratio": 100},
                                  {"symbol": "AAPL240426C00162500", "side": "sell", "ratio": 1}]}
CHANGE_BODIES = {
    ("POST", "/api/accounts"): {"name": "Second", "type": "cash", "starting_cash": "1000"},
    ("POST", "/api/accounts/{aid}/orders/preview"): {"symbol": "AAPL", "side": "buy", "qty": 1},
    ("POST", "/api/accounts/{aid}/orders"): {"symbol": "AAPL", "side": "buy", "qty": 1},
    ("POST", "/api/accounts/{aid}/multileg/preview"): COVERED_CALL,
    ("POST", "/api/accounts/{aid}/multileg"): COVERED_CALL,
    ("PUT", "/api/watchlists/{name}"): ["AAPL", "MSFT"],
    ("POST", "/api/alerts"): {"symbol": "AAPL", "op": "above", "price": "500"},
    ("POST", "/api/alert-rules"): {"kind": "indicator", "symbol": "AAPL", "indicator": "sma", "period": 5,
                                   "tf": "1m", "op": "crosses_above"},
    ("PATCH", "/api/alert-rules/{alert_id}"): {"period": 7, "tf": "1d"},
    ("PUT", "/api/accounts/{aid}/lot-method"): {"method": "lifo"},
}


def test_no_route_asks_the_data_provider_while_holding_the_lock():
    """A live feed fetches bars, chains and research over the network. None of that may happen under the lock."""
    from app.marketdata.alpaca import AlpacaFeed
    from app.marketdata.finnhub import FinnhubClient
    from app.marketdata.research import ResearchHub
    from test_alpaca import FakeTransport, Wall, contracts_for, raw, routes, ts

    under_lock = []

    class Watching(FakeTransport):
        def get(self, url, headers, timeout):
            if held():
                under_lock.append(url.split("?")[0])
            return super().get(url, headers, timeout)

    t = "2024-04-22T15:00:00Z"
    wall = Wall(ts(t))
    tr = Watching(routes(**{"/v2/options/contracts": contracts_for("2024-04-26"),
                            "/api/v1/calendar/earnings": raw("finnhub/earnings_calendar.json"),
                            "/api/v1/stock/profile2": raw("finnhub/company_profile2.json"),
                            "/api/v1/stock/metric": raw("finnhub/basic_financials.json")}))
    feed = AlpacaFeed("k", "s", transport=tr, sleep=lambda s: None, wall_clock=wall)
    feed.load_assets()
    feed.research = ResearchHub(alpaca=feed.rest, finnhub=FinnhubClient("f", transport=tr, sleep=lambda s: None), clock=wall)
    ex = Exchange(feed, SimClock(ts(t)), session_mode="us")
    aid = ex.create_account("Margin", "margin").id
    feed.apply_snapshots(json.loads(raw("alpaca/stock_snapshots.json"), parse_float=D))
    # The saved snapshot's quote is from 2022, which counts as stale (market orders are refused): a fresh one
    feed._on_stream([{"T": "q", "S": "AAPL", "bp": D("172.60"), "bs": 1, "ap": D("172.70"), "as": 1, "t": t},
                     {"T": "t", "S": "AAPL", "p": D("172.65"), "s": 100, "t": t}])
    ex.tick()
    app = create_app(ex, run_loop=False)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post(f"/api/accounts/{aid}/orders", json={"symbol": "AAPL", "side": "buy", "qty": 1}).status_code == 200
    asked = 0
    for method, path, _ in api_routes(app):
        if method != "GET":
            continue
        url = fill_path(path, aid).replace("MKTX", "AAPL").replace("2026-11", "2024-03")
        n = len(tr.requests)
        c.get(url, params={"expiry": "2024-04-26", "benchmark": "AAPL"})
        asked += len(tr.requests) > n
    c.get(f"/api/accounts/{aid}/statements/2024-04")
    ex.tick()
    assert c.post(f"/api/accounts/{aid}/orders/preview", json={"symbol": "AAPL", "side": "buy", "qty": 1}).status_code == 200
    assert asked >= 6, "the routes that fetch from the provider were not exercised"
    assert under_lock == []
    # An option order with the provider's contract list not cached (or over an hour old): the list is fetched
    # before the lock is taken, never inside the order routes (release review finding 2).
    listing = feed.option_listing("AAPL")
    expiry = sorted(listing)[0]
    contract = build("AAPL", expiry, "C", sorted(listing[expiry])[len(listing[expiry]) // 2])
    for path in ("orders/preview", "orders"):
        feed._exp_cache.clear()
        n = len(tr.requests)
        r = c.post(f"/api/accounts/{aid}/{path}", json={"symbol": contract, "side": "buy", "qty": 1, "type": "limit",
                                                       "limit_price": "0.05", "tif": "day"})
        assert r.status_code == 200, r.text
        assert any(q["path"] == "/v2/options/contracts" for q in tr.requests[n:]), "the listing was not fetched at all"
    assert under_lock == []
    # The poller thread learns what to fetch through the lock, and gets the last list while the lock is busy.
    ctx = app.state.ctx
    assert feed.interest_source == ctx.live_interest and "AAPL" in feed.wanted()
    # Discovery (PR 20) streams only lists shown recently while any is shown; the route sweep above showed
    # "Main", so forget that here: this test is about the lock, not about which lists are streamed.
    app.state.discovery["shown"].clear()
    with Holder(ctx.lock):
        ex.watchlists["Later"] = ["MSFT"]
        t0 = time.monotonic()
        assert "AAPL" in ctx.live_interest() and "MSFT" not in ctx.live_interest()
        assert time.monotonic() - t0 < 3
    assert "MSFT" in ctx.live_interest()
    # Every route that changes something, with a body that gets it past validation where one is needed.
    # An indicator alert first, so the PATCH below edits one (fill_path's alert id is the first alert).
    assert c.post("/api/alert-rules", json=CHANGE_BODIES[("POST", "/api/alert-rules")]).json()["id"] == "AL00001"
    walked, ok = 0, set()
    for method, path, _ in sorted(api_routes(app), key=lambda r: (r[0] == "DELETE" or "/reset" in r[1]
                                                                   or r[1].startswith("/api/replay/"))):
        if method == "GET":
            continue
        url = fill_path(path, aid).replace("MKTX", "AAPL").replace("2026-11", "2024-03")
        r = c.request(method, url, json=CHANGE_BODIES.get((method, path), {}), params={"account_id": aid})
        walked += 1
        if r.status_code == 200:
            ok.add((method, path))
    assert walked >= 30
    must_work = {k for k in CHANGE_BODIES if k[0] != "DELETE"}
    assert must_work <= ok, f"the body table no longer gets these past validation: {sorted(must_work - ok)}"
    assert under_lock == []


def test_alerts_never_ask_the_data_provider_while_holding_the_lock():
    """Indicator and volume alerts need price history. With a live feed that is a provider request, so the
    alert engine may only read what the feed already has in memory: creating an alert and evaluating it in
    the tick make no request at all. The feed's history loader fetches outside the lock, and the alert is
    seeded and evaluated on a later tick."""
    from datetime import datetime, timezone
    from app.marketdata.alpaca import AlpacaFeed
    from test_alpaca import FakeTransport, Wall, raw, routes, ts

    under_lock, closes = [], {}

    class Watching(FakeTransport):
        def get(self, url, headers, timeout):
            if held():
                under_lock.append(url.split("?")[0])
            return super().get(url, headers, timeout)

    def iso(t):
        return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")

    def bars(req):  # closed bars up to the feed's wall clock: flat at 100 unless `closes` says otherwise
        step = 86400 if req["params"]["timeframe"] in ("1Day", "1Week") else 60
        last = wall.t // step * step - step
        rows = [{"t": iso(last - step * i), "o": 100, "h": 100, "l": 100, "c": closes.get(last - step * i, 100),
                 "v": 1000} for i in range(int(req["params"]["limit"]))]
        return 200, json.dumps({"bars": {"AAPL": rows}, "next_page_token": None})

    t0 = ts("2024-04-22T15:00:05Z")
    wall = Wall(t0)
    tr = Watching(routes(**{"/v2/stocks/bars": bars}))
    feed = AlpacaFeed("k", "s", transport=tr, sleep=lambda s: None, wall_clock=wall)
    feed.load_assets()
    clock = SimClock(t0)
    ex = Exchange(feed, clock, session_mode="us")
    aid = ex.create_account("Margin", "margin").id
    snaps = json.loads(raw("alpaca/stock_snapshots.json"), parse_float=D)
    for snap in snaps.values():  # the saved quotes, as if they had just arrived
        snap["latestQuote"]["t"] = snap["latestTrade"]["t"] = iso(t0 - 1)
        snap["dailyBar"]["t"], snap["prevDailyBar"]["t"] = "2024-04-22T04:00:00Z", "2024-04-19T04:00:00Z"
    feed.apply_snapshots(snaps)
    ex.tick()
    app = create_app(ex, run_loop=False)
    ctx = app.state.ctx
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post(f"/api/accounts/{aid}/orders", json={"symbol": "AAPL", "side": "buy", "qty": 1}).status_code == 200
    assert ex.accounts[aid].position("AAPL") == 1  # held, so the calendar alert has something to look up

    def tick(to=None):  # what the server's turn does: the whole tick under the lock; AAPL's quote changes
        if to is not None:
            wall.t = to
            clock.set(to)
        for snap in snaps.values():
            snap["latestQuote"]["t"] = snap["latestTrade"]["t"] = iso(wall.t - 1)
        feed.apply_snapshots(snaps)
        with ctx.lock.hold(1, urgent=True, label="tick"):
            ex.tick()

    def alert(i):
        return ex.alerting.get(ids[i])

    n = len(tr.requests)
    ids = []
    for body in ({"kind": "indicator", "symbol": "AAPL", "indicator": "sma", "period": 5, "tf": "1m", "op": "crosses_above"},
                 {"kind": "indicator", "symbol": "AAPL", "indicator": "rsi", "period": 14, "tf": "1d", "op": "above", "value": 70},
                 {"kind": "volume", "symbol": "AAPL", "value": "2", "mode": "average"},
                 {"kind": "calendar", "account_id": aid, "days_before": 5, "repeat": True}):
        r = c.post("/api/alert-rules", json=body)
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    assert c.patch(f"/api/alert-rules/{ids[1]}", json={"value": 75}).status_code == 200
    evaluated = ex.alerting.evaluations
    tick()
    tick()
    assert ex.alerting.evaluations > evaluated  # the volume and calendar alerts were looked at
    assert len(tr.requests) == n, "creating or evaluating an alert asked the provider"
    assert alert(0)["last_value"] == alert(1)["last_value"] == "waiting for price history"
    assert alert(2)["last_value"] == "no volume history yet"
    assert c.get("/api/alert-rules").json()[0]["last_value"] == "waiting for price history"

    # The history thread's work, done here by hand: outside the lock.
    assert feed.load_wanted_bars() == 2 and len(tr.requests) > n  # 1m and 1d history for AAPL
    assert feed.load_wanted_bars() == 0
    n = len(tr.requests)
    tick()  # seeds both series and the average volume from memory
    assert alert(2)["last_value"] != "no volume history yet"
    assert all(s.seeded for s in ex.alerting._series.values())
    # A minute bar closes below its average, the next one above it: each is read after its own load.
    minute = t0 // 60 * 60
    closes[minute] = 99
    tick(minute + 65)
    tick()
    assert len(tr.requests) == n and feed.load_wanted_bars() == 1
    tick()
    assert alert(0)["status"] == "active" and alert(0)["last_value"] not in (None, "waiting for price history")
    closes[minute + 60] = 105
    tick(minute + 125)
    assert alert(0)["status"] == "active"  # the closed bar has not been loaded yet: not evaluated
    assert feed.load_wanted_bars() == 1
    n = len(tr.requests)
    tick()
    assert alert(0)["status"] == "triggered" and alert(0)["fire_count"] == 1
    assert len(tr.requests) == n
    assert under_lock == []


# ---- the tick ---------------------------------------------------------------------------------------------
def wait_for(cond, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.005)
    return cond()


def test_a_stuck_request_delays_the_tick_but_cannot_break_or_block_the_server(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(api_locking, "ROUTE_WAIT", 0.05)
    monkeypatch.setattr("app.api.server.TICK_WAIT", 0.05)
    c, app, ex = make_app(tmp_path, run_loop=True, tick_seconds=0.005)
    ctx, aid = app.state.ctx, list(ex.accounts)[0]
    with c:
        assert wait_for(lambda: ctx.metrics["turns"] >= 3)
        with Holder(ctx.lock, "POST /api/slow"):
            turns = ctx.metrics["turns"]
            assert wait_for(lambda: ctx.metrics["skipped"] >= 3)
            t0 = time.monotonic()
            assert c.post(f"/api/accounts/{aid}/orders", json={"symbol": "MKTX", "side": "buy", "qty": 1}).status_code == 503
            assert time.monotonic() - t0 < 2  # a bounded wait, then a clear answer
            assert c.get("/api/auth/state").status_code == 200  # the event loop itself is not blocked
            assert ctx.metrics["turns"] == turns
        assert wait_for(lambda: ctx.metrics["turns"] >= turns + 3), "the market did not resume"
        assert c.post(f"/api/accounts/{aid}/orders", json={"symbol": "MKTX", "side": "buy", "qty": 1}).status_code == 200
    out = capsys.readouterr().out
    assert "tick skipped: the exchange lock is held by POST /api/slow" in out and out.count("tick skipped") == 1
    assert "tick error" not in out and ctx.metrics["errors"] == 0


def test_requests_cannot_starve_the_tick(tmp_path):
    """Eight threads take the lock back to back; the tick still gets in after almost every one of them."""
    c, app, ex = make_app(tmp_path, run_loop=True, tick_seconds=0.01)
    ctx = app.state.ctx
    stop, holds = threading.Event(), [0]

    def hog():
        while not stop.is_set():
            with ctx.locked("hog"):
                holds[0] += 1
                time.sleep(0.03)  # a slow request; longer than any timer step (Windows: about 16 ms)

    with c:
        hogs = [threading.Thread(target=hog, daemon=True) for _ in range(8)]
        for h in hogs:
            h.start()
        assert wait_for(lambda: holds[0] >= 8)
        turns0, holds0 = ctx.metrics["turns"], holds[0]
        assert wait_for(lambda: holds[0] >= holds0 + 60, 30)
        turns, held_n = ctx.metrics["turns"] - turns0, holds[0] - holds0
        stop.set()
        for h in hogs:
            h.join(5)
    assert ctx.metrics["skipped"] == 0 and ctx.metrics["errors"] == 0
    # Waiting in line like everyone else, the tick would get one turn in nine (about 13 here).
    assert turns >= held_n / 3, f"{turns} turns while requests took the lock {held_n} times"


def test_a_tick_that_raises_is_reported_and_the_market_goes_on(tmp_path, capsys):
    c, app, ex = make_app(tmp_path, run_loop=True, tick_seconds=0.002)
    ctx = app.state.ctx
    tick, calls = ex.tick, []

    def flaky():
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("boom")
        return tick()
    ex.tick = flaky
    with c:
        assert wait_for(lambda: ctx.metrics["turns"] >= 6)
        assert not ctx.lock.holder() or ctx.lock.holder()[0] == "tick"
    assert ctx.metrics["errors"] == 1 and "tick error: RuntimeError('boom')" in capsys.readouterr().out


# ---- saving ---------------------------------------------------------------------------------------------------
def test_a_fault_while_building_the_broadcast_does_not_cost_the_snapshot(tmp_path):
    c, app, ex = make_app(tmp_path, run_loop=True, tick_seconds=0.005)
    ctx, aid = app.state.ctx, list(ex.accounts)[0]
    path = os.path.join(ex.data_dir, "state.json")
    with c, c.websocket_connect("/ws") as ws:
        ws.send_json({"symbols": ["MKTX"], "account": aid})
        assert wait_for(lambda: ctx.clients and list(ctx.clients.values())[0]["account"] == aid)
        ex.positions = lambda account_id: (_ for _ in ()).throw(RuntimeError("fault in the broadcast"))
        assert c.post(f"/api/accounts/{aid}/orders", json={"symbol": "MKTX", "side": "buy", "qty": 1}).status_code == 200

        def saved():
            with open(path, encoding="utf-8") as f:
                return len(json.load(f)["orders"])
        assert wait_for(lambda: os.path.exists(path) and saved() == 1), "the order never reached state.json"
        assert ctx.metrics["errors"] > 0


def test_a_bad_account_from_a_websocket_client_breaks_nothing(tmp_path):
    """An unhashable account id is dropped; the other client still gets its messages, the state is saved."""
    c, app, ex = make_app(tmp_path, run_loop=True, tick_seconds=0.005)
    ctx, aid = app.state.ctx, list(ex.accounts)[0]
    path = os.path.join(ex.data_dir, "state.json")
    with c, c.websocket_connect("/ws") as bad, c.websocket_connect("/ws") as good:
        bad.send_json({"symbols": ["MKTX"], "account": []})
        good.send_json({"symbols": ["MKTX"], "account": aid})
        assert wait_for(lambda: len(ctx.clients) == 2 and aid in [v["account"] for v in ctx.clients.values()])
        assert None in [v["account"] for v in ctx.clients.values()]
        assert c.post(f"/api/accounts/{aid}/orders", json={"symbol": "MKTX", "side": "buy", "qty": 1}).status_code == 200
        assert wait_for(lambda: os.path.exists(path) and len(json.load(open(path, encoding="utf-8"))["orders"]) == 1)
        for _ in range(50):
            msg = good.receive_json()
            if msg.get("positions"):
                break
        assert msg["positions"][0]["symbol"] == "MKTX"
    assert ctx.metrics["errors"] == 0


def test_a_turn_that_fails_after_the_replay_advanced_does_not_run_the_replay_fast(tmp_path):
    """Wall time passed to the replay is counted once, even when the turn raises after advancing it."""
    c, app, ex = make_app(tmp_path, run_loop=True, tick_seconds=0.01, replay_tick_seconds=0.01)
    ctx = app.state.ctx
    with c:
        assert c.post("/api/replay/start", json={"session": SESSION.split(".")[0]}).status_code == 200
        rp = ctx.replay
        given, real = [], rp.advance

        def advance(elapsed):
            given.append(elapsed)
            real(elapsed)
            raise RuntimeError("fault after the replay advanced")
        rp.advance = advance
        t0 = time.monotonic()
        assert wait_for(lambda: len(given) >= 20, 10)
        wall = time.monotonic() - t0
        rp.advance = real
    assert ctx.metrics["errors"] >= 20
    # Without the fix every failed turn passes the whole time since the start again.
    assert sum(given) <= wall + 0.5, f"{sum(given):.2f} s given to the replay over {wall:.2f} s"


def test_snapshot_is_taken_in_memory_and_an_older_one_never_overwrites_a_newer_one(tmp_path):
    # Merged with PR 19: a plan is a storage.store.SavePlan (state document + segment lines), not file texts.
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("t", "margin", D("100000"))
    first = ex.save_plan(force=True)
    assert not os.path.exists(tmp_path / "state.json"), "save_plan() must not touch the disk"
    assert first.state is not None and not ex.dirty
    # the snapshot is bytes: later changes to live state cannot leak into it
    ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 10})
    written = first.state + b"".join(line for seg in first.segments for line in seg.lines)
    assert b"ABC" not in written
    assert ex.dirty
    second = ex.save_plan()
    assert ex.write_plan(second)
    assert ex.write_plan(first)  # a slow writer arriving late: skipped
    back = Exchange(feed, clock, data_dir=str(tmp_path))
    assert back.load()
    assert len(back.engine.orders) == 1 and len(back.accounts[a.id].fills) == 1
    assert ex.save_plan() is None  # nothing changed, nothing to write
    assert ex.save(force=True)
    again = Exchange(feed, clock, data_dir=str(tmp_path))
    assert again.load() and again.to_document()["orders"] == back.to_document()["orders"]


def test_a_failed_write_leaves_the_exchange_dirty_so_the_next_save_retries(tmp_path, monkeypatch):
    from app.storage import fsutil
    ex, feed, clock = make_exchange(str(tmp_path))
    ex.create_account("t", "margin", D("100000"))
    files = ex.save_plan()
    monkeypatch.setattr(fsutil, "atomic_write_bytes", lambda path, data: (_ for _ in ()).throw(OSError("disk full")))
    assert ex.write_plan(files) is False  # PR 19: a failed write answers False and shows in the save status
    assert ex.dirty and ex.store.status()["saving_failed"]
    monkeypatch.undo()
    assert ex.save(force=True)
    assert json.loads((tmp_path / "state.json").read_text())["accounts"]


def test_load_changes_nothing_when_the_file_is_unusable(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("t", "margin", D("100000"))
    ex.save(force=True)
    data = json.loads((tmp_path / "state.json").read_text())
    data["accounts"][0]["starting_cash"] = "lots"  # PR 19 migrates a missing counter; this cannot be repaired
    (tmp_path / "state.json").write_text(json.dumps(data))
    ex2, _, _ = make_exchange(str(tmp_path))
    keep = ex2.create_account("mine", "cash", D("5"))
    assert ex2.load() is False  # PR 19: a damaged file puts the store in recovery instead of raising
    assert ex2.store.blocked
    assert list(ex2.accounts) == [keep.id] and a.id == keep.id  # still the account it had, not half of the file


def test_the_tick_loop_saves_off_the_lock_and_the_file_matches_memory(tmp_path):
    c, app, ex = make_app(tmp_path, run_loop=True, tick_seconds=0.002)
    ctx, aid = app.state.ctx, list(ex.accounts)[0]
    writes = []
    write = ex.write_plan

    def watched(files):
        writes.append((ctx.lock.holder(), held(), threading.current_thread().name))
        return write(files)
    ex.write_plan = watched
    with c:
        for i in range(10):
            assert c.post(f"/api/accounts/{aid}/orders", json={"symbol": "MKTX", "side": "buy", "qty": 1}).status_code == 200
        assert wait_for(lambda: len(writes) >= 2)
    assert writes and not any(h for _, h, _ in writes), "a state file was written while holding the exchange lock"
    assert any(name.startswith("paperdesk-save") for _, _, name in writes)
    saved = json.loads((tmp_path / "state.json").read_text())
    assert len(saved["orders"]) == 10 == len(ex.engine.orders) and not ex.dirty


# ---- atomic changes -----------------------------------------------------------------------------------------------
def book(ex, a):
    return json.dumps({"account": a.to_plain(), "orders": {k: vars(o) for k, o in ex.engine.orders.items()},
                       "events": len(ex.events), "fill_seq": ex._peek_sequences()["fill"],
                       "notifications": ex.alerting.notices.to_plain()}, default=str, sort_keys=True)


def test_a_multi_leg_submit_that_fails_on_its_second_leg_leaves_nothing_behind():
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "margin", D("100000"))
    ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 10})
    ex.submit(a.id, {"symbol": "ABC", "side": "sell", "qty": 5, "type": "limit", "limit_price": "150.00", "tif": "gtc"})
    expiry = chain.expirations(cal.ny_date(T0))[2]
    legs = [{"symbol": build("ABC", expiry, "C", D(100)), "side": "buy"}, {"symbol": build("ABC", expiry, "C", D(105)), "side": "sell"}]
    before = book(ex, a)
    apply_fill, n = a.apply_fill, []

    def second_leg_fails(fill, mult, *args, **kw):  # PR 13 passes the named and reserved lots too
        n.append(fill)
        if len(n) == 2:
            raise RuntimeError("fault while booking the second leg")
        return apply_fill(fill, mult, *args, **kw)

    a.apply_fill = second_leg_fails
    with pytest.raises(RuntimeError):
        ex.submit(a.id, {"legs": legs, "qty": 2, "client_id": "combo"})
    del a.apply_fill
    assert len(n) == 2, "the first leg was booked before the fault"
    assert book(ex, a) == before and problems(ex) == []
    assert not [o for o in ex.engine.orders.values() if o.client_id == "combo"]
    # the same request then goes through whole, and its fills continue the fill numbering without a gap
    [o] = ex.submit(a.id, {"legs": legs, "qty": 2, "client_id": "combo"})
    assert o.status == "filled" and [f.id for f in a.fills] == ["F00000001", "F00000002", "F00000003"]
    assert problems(ex) == []


def test_a_fault_after_the_fill_of_a_bracket_entry_rolls_the_whole_request_back():
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "margin", D("100000"))
    before = book(ex, a)
    on_fill = ex.lifecycle.on_fill
    ex.lifecycle.on_fill = lambda acct, fill: (_ for _ in ()).throw(RuntimeError("fault after booking"))
    with pytest.raises(RuntimeError):
        ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 10, "take_profit": "110.00", "stop_loss": "90.00"})
    ex.lifecycle.on_fill = on_fill
    assert book(ex, a) == before and a.position("ABC") == 0 and a.cash == D("100000") and not ex.engine.orders
    assert len(ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 10, "take_profit": "110.00", "stop_loss": "90.00"})) == 3
    assert problems(ex) == []


def test_a_refused_replace_changes_no_field_of_the_order():
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "margin", D("100000"))
    ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 10})
    [stop] = ex.submit(a.id, {"symbol": "ABC", "side": "sell", "qty": 10, "type": "stop", "stop_price": "90.00", "tif": "gtc"})
    before = book(ex, a)
    with pytest.raises(ValueError, match="no limit price"):  # the quantity is valid, the limit price is not
        ex.replace(a.id, stop.id, {"qty": "4", "limit_price": "95.00"})
    assert stop.qty == 10 and book(ex, a) == before
    assert ex.replace(a.id, stop.id, {"qty": "4"}).qty == 4


def test_cancel_all_is_all_or_nothing():
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "margin", D("100000"))
    for px in ("90.00", "91.00", "92.00"):
        ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "limit", "limit_price": px, "tif": "gtc"})
    before = book(ex, a)
    cancel, n = ex.engine.cancel, []

    def third_fails(*args, **kw):
        n.append(1)
        if len(n) == 3:
            raise RuntimeError("fault")
        return cancel(*args, **kw)
    ex.engine.cancel = third_fails
    with pytest.raises(RuntimeError):
        ex.cancel_all(a.id)
    del ex.engine.cancel
    assert book(ex, a) == before and len(ex.engine.active_orders()) == 3
    assert ex.cancel_all(a.id) == 3


def test_a_rejected_submit_takes_no_rollback_snapshot(monkeypatch):
    """Validation and the risk and rule checks change nothing, so a rejected order costs no copy."""
    from app.exchange import OrderRejected
    from app.lifecycle.state import AccountLife
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "cash", D("1000"))
    copies = []
    real = AccountLife.copy
    monkeypatch.setattr(AccountLife, "copy", lambda self: copies.append(1) or real(self))
    before = book(ex, a)
    with pytest.raises(OrderRejected):
        ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 1000})  # far more than the cash
    with pytest.raises(OrderRejected):
        ex.submit(a.id, {"symbol": "NOPE", "side": "buy", "qty": 1})
    assert copies == [] and book(ex, a) == before
    ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 1})
    assert copies == [1]


def test_a_rolled_back_request_keeps_a_failed_write_mark():
    """The save thread sets `dirty` when a write fails; a request failing at the same moment must not clear it."""
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "margin", D("100000"))
    ex.dirty = False
    handle = ex._handle

    def save_fails_meanwhile(events):
        ex.dirty = True  # what write_plan() does on another thread when the disk is full
        raise RuntimeError("fault in the request")
    ex._handle = save_fails_meanwhile
    with pytest.raises(RuntimeError):
        ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 1})
    ex._handle = handle
    assert ex.dirty and not ex.engine.orders


def test_a_failed_market_snapshot_write_is_retried_on_the_next_turn(tmp_path, monkeypatch):
    from app.storage import fsutil
    ex = Exchange(SyntheticFeed(seed=3, now=T0), SimClock(T0), data_dir=str(tmp_path))
    ex.create_account("t", "margin", D("1000"))
    ex.tick()
    plan = ex.save_plan(force=True)
    assert plan.market is not None
    real = fsutil.atomic_write_bytes

    def full(path, data):
        raise OSError("disk full")
    monkeypatch.setattr(fsutil, "atomic_write_bytes", full)
    assert ex.write_plan(plan) is False
    monkeypatch.setattr(fsutil, "atomic_write_bytes", real)
    assert ex.dirty
    again = ex.save_plan(force=True)  # no simulated time has passed; forced past the back-off of the failed save
    assert again.state is not None and again.market is not None
    assert ex.write_plan(again)
    assert os.path.exists(os.path.join(str(tmp_path), "market.json"))


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.tuples(st.sampled_from(["limit", "market", "cancel", "move", "cancel_all", "fail"]),
                          st.integers(0, 6)), min_size=1, max_size=25))
def test_the_open_order_index_always_matches_a_walk_of_every_order(steps):
    """active_orders() reads an index instead of every order; it must give what the full walk gives, in order."""
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "margin", D("100000"))
    b = ex.create_account("u", "margin", D("100000"))
    for kind, n in steps:
        acct = (a, b)[n % 2]
        try:
            if kind == "limit":
                ex.submit(acct.id, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "limit",
                                    "limit_price": f"{95 + n}.00", "tif": "gtc"})
            elif kind == "market":
                ex.submit(acct.id, {"symbol": "ABC", "side": "buy", "qty": 1, "take_profit": "130.00",
                                    "stop_loss": "70.00"})
            elif kind == "cancel":
                open_ = [o for o in ex.engine.active_orders() if o.account_id == acct.id]
                if open_:
                    ex.cancel(acct.id, open_[n % len(open_)].id)
            elif kind == "move":
                move(ex, feed, clock, "ABC", f"{94 + n * 2}.00")
            elif kind == "cancel_all":
                ex.cancel_all(acct.id)
            else:  # a request that fails after its fill and is rolled back
                on_fill = ex.lifecycle.on_fill
                ex.lifecycle.on_fill = lambda *x: (_ for _ in ()).throw(RuntimeError("fault"))
                try:
                    ex.submit(acct.id, {"symbol": "ABC", "side": "buy", "qty": 1})
                except RuntimeError:
                    pass
                finally:
                    ex.lifecycle.on_fill = on_fill
        except (ValueError, KeyError):
            pass
        assert ex.engine.active_orders() == [o for o in ex.engine.orders.values() if o.active]
        assert ex.engine.active_orders("ABC") == [o for o in ex.engine.orders.values() if o.active]
    ex.reset_account(b.id)
    assert ex.engine.active_orders() == [o for o in ex.engine.orders.values() if o.active]
    assert problems(ex) == []


@settings(max_examples=60, deadline=None)
@given(st.lists(st.tuples(st.text(max_size=3), st.decimals(-1000, 1000, places=2), st.text(max_size=3)), max_size=8),
       st.dictionaries(st.text(max_size=3), st.lists(st.tuples(st.text(max_size=3), st.decimals(0, 10, places=0),
                                                               st.booleans()).map(list), max_size=3), max_size=3),
       st.lists(st.lists(st.text(max_size=3), max_size=3), max_size=4))
def test_account_life_copy_is_equal_and_independent(unsettled, opened, log):
    from app.lifecycle.state import AccountLife
    life = AccountLife(unsettled=[list(u) for u in unsettled], opened=opened, log=log,
                       accrual_rates={"interest": {"0.05": [D("1.25"), 3]}},
                       pending_dividends=[{"symbol": "ABC", "amount": "1.00"}])
    before = json.dumps(life.to_plain(), sort_keys=True, default=str)
    c = life.copy()
    assert json.dumps(c.to_plain(), sort_keys=True, default=str) == before
    for lots in c.opened.values():
        for lot in lots:
            lot[1] = D(-1)
    for v in (c.unsettled, c.log, c.pending_dividends, *c.opened.values(), *c.unsettled, *c.log):
        v.append("x")
    c.accrual_rates["interest"]["0.05"][0] = D(0)
    c.pending_dividends[0]["amount"] = "9"
    assert json.dumps(life.to_plain(), sort_keys=True, default=str) == before


# ---- replay commands ------------------------------------------------------------------------------------------------
def test_a_long_seek_holds_the_lock_only_in_short_slices(tmp_path, monkeypatch):
    monkeypatch.setattr("app.marketdata.replay.control.SLICE_SECONDS", 0.001)  # many slices even on a fast machine
    c, app, ex = make_app(tmp_path)
    ctx = app.state.ctx
    assert c.post("/api/replay/start", json={"session": SESSION.split(".")[0]}).status_code == 200
    rp, sandbox = ctx.replay, list(ctx.ex.accounts)[0]
    assert rp.guard is ctx.lock
    tick = rp.ex.tick
    rp.ex.tick = lambda: (time.sleep(0.0001), tick())[1]  # a market slow enough that the seek takes a while
    ctx.lock.longest = (0.0, None)
    t, out = in_thread(lambda: c.post("/api/replay/seek", json={"offset": 3600}))
    served = 0
    while t.is_alive():  # other requests are answered while the seek runs
        assert c.get(f"/api/accounts/{sandbox}").status_code == 200
        assert c.post(f"/api/accounts/{sandbox}/orders", json={"symbol": "MKTX", "side": "buy", "qty": 1}).status_code == 200
        served += 1
    t.join(30)
    assert out["value"].status_code == 200 and rp.now() == rp.start + 3600
    assert served >= 3, "the seek shut every other request out"
    assert ctx.lock.longest[0] < 0.5, ctx.lock.longest
    assert problems(ctx.ex) == []


def test_step_under_the_lock_never_waits_for_the_command_lock_and_exit_stops_a_running_seek(tmp_path):
    path = os.path.join(BUNDLED_DIR, SESSION)
    rp = ReplayController(path)
    with rp.guard:
        assert rp.step(5) == 5  # called while holding the exchange lock: runs in one piece
    busy = threading.Event()
    release = threading.Event()

    def other_command():
        with rp._lock:
            busy.set()
            release.wait(10)
    t = threading.Thread(target=other_command, daemon=True)
    t.start()
    assert busy.wait(5)
    with rp.guard:
        with pytest.raises(ValueError, match="still running"):
            rp.step(5)  # would deadlock if it waited: the other command needs the exchange lock to go on
        assert rp.advance(1.0) == 0 or rp.paused  # the tick skips its turn the same way
    release.set()
    t.join(5)
    # exit while a seek is running: the seek stops at its next slice instead of ticking a detached sandbox
    c, app, ex = make_app(tmp_path)
    ctx = app.state.ctx
    assert c.post("/api/replay/start", json={"session": SESSION.split(".")[0]}).status_code == 200
    rp = ctx.replay
    tick, ticks = rp._tick, [0]

    def slow_tick():  # 2 ms a simulated second: the 3.5 h seek cannot finish before the exit (about 25 s)
        ticks[0] += 1
        time.sleep(0.002)
        tick()
    rp._tick = slow_tick
    seeking, out = in_thread(lambda: rp.seek(rp.end))
    assert wait_for(lambda: ticks[0] > 0)
    assert c.post("/api/replay/exit").status_code == 200
    after_exit = ticks[0]
    seeking.join(10)
    assert not seeking.is_alive() and rp.closed and ctx.ex is ex
    # Stopped at its next slice: no tick of the detached sandbox once the exit has answered.
    assert ticks[0] == after_exit and rp.now() < rp.end and out["value"] < rp.end - rp.start


def test_a_start_waits_until_an_exit_has_written_the_sandbox(tmp_path, monkeypatch):
    """The exit writes the sandbox after releasing the exchange lock; a start from a second browser at that
    moment would remove the same directory. It waits for the exit instead, and then starts cleanly."""
    c, app, ex = make_app(tmp_path)
    ctx = app.state.ctx
    session = SESSION.split(".")[0]
    assert c.post("/api/replay/start", json={"session": session}).status_code == 200
    sandbox = ctx.ex
    writing, go_on, order = threading.Event(), threading.Event(), []
    real = sandbox.write_plan

    def slow_write(files):
        writing.set()
        go_on.wait(10)
        order.append("exit wrote")
        real(files)
    sandbox.write_plan = slow_write
    exiting, out_exit = in_thread(lambda: c.post("/api/replay/exit").status_code)
    assert writing.wait(5) and ctx.replay is None  # the exchange lock is released, the files are not written
    starting, out_start = in_thread(lambda: (order.append("start"), c.post("/api/replay/start",
                                                                            json={"session": session}).status_code)[1])
    time.sleep(0.2)
    assert ctx.replay is None, "the start ran while the exit was still writing the sandbox"
    go_on.set()
    exiting.join(10)
    starting.join(10)
    assert out_exit["value"] == 200 and out_start["value"] == 200
    assert order == ["start", "exit wrote"] and ctx.replay is not None
    assert c.post("/api/replay/exit").status_code == 200


def test_a_replay_starts_even_when_the_live_state_cannot_be_written(tmp_path, monkeypatch):
    c, app, ex = make_app(tmp_path)
    ctx = app.state.ctx
    real = ex.write_plan

    def full(files):
        ex.dirty = True
        raise OSError("disk full")
    ex.write_plan = full
    r = c.post("/api/replay/start", json={"session": SESSION.split(".")[0]})
    assert r.status_code == 200 and ctx.replay is not None and ex.dirty  # live is saved again later
    ex.write_plan = real
    assert c.post("/api/replay/exit").status_code == 200 and ctx.ex is ex


def test_load_waits_out_a_file_briefly_held_by_another_program_on_windows(tmp_path, monkeypatch):
    """On Windows, opening state.json while the save thread replaces it (or a virus scanner holds it)
    raises PermissionError. load() must not take that for "no saved state" and start empty."""
    import builtins
    import app.exchange as exchange_mod
    ex = Exchange(SyntheticFeed(seed=3, now=T0), SimClock(T0), data_dir=str(tmp_path))
    ex.create_account("Kept", "margin", D("1234"))
    ex.save(force=True)
    real_open, refused = builtins.open, []

    def held_open(path, *a, **kw):
        if str(path).endswith("state.json") and len(refused) < 3:
            refused.append(path)
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **kw)
    monkeypatch.setattr(exchange_mod.os, "name", "nt")
    monkeypatch.setattr(builtins, "open", held_open)
    again = Exchange(SyntheticFeed(seed=3, now=T0), SimClock(T0), data_dir=str(tmp_path))
    assert again.load() and [a.name for a in again.accounts.values()] == ["Kept"] and len(refused) == 3
    # Elsewhere a PermissionError is a real permission problem: no waiting, the error is not hidden.
    monkeypatch.setattr(exchange_mod.os, "name", "posix")
    refused.clear()
    with pytest.raises(PermissionError):
        Exchange._read_text(os.path.join(str(tmp_path), "state.json"))
    assert len(refused) == 1
