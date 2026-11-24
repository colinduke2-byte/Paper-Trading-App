"""The exchange lock and what it guarantees (docs/CONCURRENCY.md): the lock itself, which routes run
under it, bounded waits, the tick's priority, atomic order changes and ordered state files.
The many-thread stress tests are in test_concurrency_stress.py."""
import json
import os
import threading
import time

import pytest
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
    "/api/symbols", "/api/quotes", "/api/bars/{symbol}", "/api/depth/{symbol}", "/api/trades/{symbol}",
    "/api/fundamentals/{symbol}", "/api/movers", "/api/screener", "/api/options/expirations/{symbol}",
    "/api/options/chain/{symbol}",  # market data: the feed only, and a live feed asks the provider
    "/api/accounts/{aid}/analytics", "/api/accounts/{aid}/statements/{month}",
    "/api/accounts/{aid}/statements/{month}/html",  # fetch price history, then read the account under ctx.locked()
    "/api/replay/sessions",  # reads session files
    "/api/auth/state", "/api/research/sources", "/api/research/news/{symbol}", "/api/research/calendar/{symbol}",
    "/api/research/corporate-actions/{symbol}", "/api/research/fundamentals/{symbol}",
}
SELF_LOCKING_WRITES = {  # take ctx.lock themselves, in pieces: proven by test_every_changing_route_answers_busy...
    ("POST", "/api/replay/start"), ("POST", "/api/replay/step"), ("POST", "/api/replay/seek"), ("POST", "/api/replay/exit"),
}
NO_EXCHANGE_STATE = {("POST", "/api/auth/login"), ("POST", "/api/auth/logout")}  # the session store has its own lock


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
                        ("{kind}", "fills"), ("{jid}", "J1"), ("{alert_id}", "AL00001"), ("{name}", "Main")):
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
            r = c.request(method, url, json=[] if path.startswith("/api/watchlists") else body)
            if path == "/api/replay/start":  # refused before it needs the lock; checked without a replay below
                assert r.status_code == 409
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
    # The poller thread learns what to fetch through the lock, and gets the last list while the lock is busy.
    ctx = app.state.ctx
    assert feed.interest_source == ctx.live_interest and "AAPL" in feed.wanted()
    with Holder(ctx.lock):
        ex.watchlists["Later"] = ["MSFT"]
        t0 = time.monotonic()
        assert "AAPL" in ctx.live_interest() and "MSFT" not in ctx.live_interest()
        assert time.monotonic() - t0 < 3
    assert "MSFT" in ctx.live_interest()


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
    c, app, ex = make_app(tmp_path, run_loop=True, tick_seconds=0.001)
    ctx = app.state.ctx
    stop, holds = threading.Event(), [0]

    def hog():
        while not stop.is_set():
            with ctx.locked("hog"):
                holds[0] += 1
                time.sleep(0.003)  # a slow request holding the lock

    with c:
        hogs = [threading.Thread(target=hog, daemon=True) for _ in range(8)]
        for h in hogs:
            h.start()
        assert wait_for(lambda: holds[0] >= 8)
        turns0, holds0 = ctx.metrics["turns"], holds[0]
        assert wait_for(lambda: holds[0] >= holds0 + 120, 30)
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
def test_snapshot_is_taken_in_memory_and_an_older_one_never_overwrites_a_newer_one(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("t", "margin", D("100000"))
    first = ex.snapshot(force=True)
    assert not os.path.exists(tmp_path / "state.json"), "snapshot() must not touch the disk"
    assert [name for name, _, _ in first] == ["state.json"] and not ex.dirty
    # the snapshot is text: later changes to live state cannot leak into it
    ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 10})
    assert json.loads(first[0][1])["orders"] == [] and ex.dirty
    second = ex.snapshot()
    ex.write_snapshot(second)
    ex.write_snapshot(first)  # a slow writer arriving late
    saved = json.loads((tmp_path / "state.json").read_text())
    assert len(saved["orders"]) == 1 and len(saved["accounts"][0]["fills"]) == 1
    assert ex.snapshot() == []  # nothing changed, nothing to write
    ex.save(force=True)
    assert json.loads((tmp_path / "state.json").read_text()) == saved


def test_a_failed_write_leaves_the_exchange_dirty_so_the_next_save_retries(tmp_path, monkeypatch):
    ex, feed, clock = make_exchange(str(tmp_path))
    ex.create_account("t", "margin", D("100000"))
    files = ex.snapshot()
    monkeypatch.setattr(Exchange, "_write_text", staticmethod(lambda path, text: (_ for _ in ()).throw(OSError("disk full"))))
    with pytest.raises(OSError):
        ex.write_snapshot(files)
    assert ex.dirty
    monkeypatch.undo()
    ex.save()
    assert json.loads((tmp_path / "state.json").read_text())["accounts"]


def test_load_changes_nothing_when_the_file_is_unusable(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("t", "margin", D("100000"))
    ex.save(force=True)
    data = json.loads((tmp_path / "state.json").read_text())
    del data["seq"]["expiry"]
    (tmp_path / "state.json").write_text(json.dumps(data))
    ex2, _, _ = make_exchange(str(tmp_path))
    keep = ex2.create_account("mine", "cash", D("5"))
    with pytest.raises(KeyError):
        ex2.load()
    assert list(ex2.accounts) == [keep.id] and a.id == keep.id  # still the account it had, not half of the file


def test_the_tick_loop_saves_off_the_lock_and_the_file_matches_memory(tmp_path):
    c, app, ex = make_app(tmp_path, run_loop=True, tick_seconds=0.002)
    ctx, aid = app.state.ctx, list(ex.accounts)[0]
    writes = []
    write = ex.write_snapshot

    def watched(files):
        writes.append((ctx.lock.holder(), held(), threading.current_thread().name))
        return write(files)
    ex.write_snapshot = watched
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
                       "events": len(ex.events), "fill_seq": ex._peek_sequences()["fill"]}, default=str, sort_keys=True)


def test_a_multi_leg_submit_that_fails_on_its_second_leg_leaves_nothing_behind():
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "margin", D("100000"))
    ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 10})
    ex.submit(a.id, {"symbol": "ABC", "side": "sell", "qty": 5, "type": "limit", "limit_price": "150.00", "tif": "gtc"})
    expiry = chain.expirations(cal.ny_date(T0))[2]
    legs = [{"symbol": build("ABC", expiry, "C", D(100)), "side": "buy"}, {"symbol": build("ABC", expiry, "C", D(105)), "side": "sell"}]
    before = book(ex, a)
    apply_fill, n = a.apply_fill, []

    def second_leg_fails(fill, mult):
        n.append(fill)
        if len(n) == 2:
            raise RuntimeError("fault while booking the second leg")
        return apply_fill(fill, mult)

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


# ---- replay commands ------------------------------------------------------------------------------------------------
def test_a_long_seek_holds_the_lock_only_in_short_slices(tmp_path, monkeypatch):
    monkeypatch.setattr("app.marketdata.replay.control.SLICE_SECONDS", 0.001)  # many slices even on a fast machine
    c, app, ex = make_app(tmp_path)
    ctx = app.state.ctx
    assert c.post("/api/replay/start", json={"session": SESSION.split(".")[0]}).status_code == 200
    rp, sandbox = ctx.replay, list(ctx.ex.accounts)[0]
    assert rp.guard is ctx.lock
    ctx.lock.longest = (0.0, None)
    t, out = in_thread(lambda: c.post("/api/replay/seek", json={"time": rp.end}))
    served = 0
    while t.is_alive():  # other requests are answered while the seek runs
        assert c.get(f"/api/accounts/{sandbox}").status_code == 200
        assert c.post(f"/api/accounts/{sandbox}/orders", json={"symbol": "MKTX", "side": "buy", "qty": 1}).status_code == 200
        served += 1
    t.join(30)
    assert out["value"].status_code == 200 and rp.now() == rp.end
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
    seeking, out = in_thread(lambda: rp.seek(rp.end))
    assert wait_for(lambda: rp.now() > rp.start)
    assert c.post("/api/replay/exit").status_code == 200
    seeking.join(10)
    assert not seeking.is_alive() and rp.closed and rp.now() < rp.end and ctx.ex is ex
