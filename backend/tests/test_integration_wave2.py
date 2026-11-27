"""Interaction tests for integration/wave2: where the features merged in this wave meet.

Merged here: backup and storage (PR 19), tax lots (PR 13), crypto (PR 14), order ticket (PR 18), discovery
(PR 20), hardening (PR 30), cross-browser (PR 11), usability polish, on top of main's thread safety (PR 29).
Each test names the two (or three) features it puts together.
"""
import json
import os
import threading
import time

from fastapi.testclient import TestClient

from app.api import locking as api_locking
from app.api.server import create_app
from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata.synthetic import SyntheticFeed
from helpers import D, T0


def make_app(tmp=None, run_loop=False, **kw):
    ex = Exchange(SyntheticFeed(seed=3, now=T0), SimClock(T0), data_dir=str(tmp) if tmp else None)
    ex.create_account("Margin", "margin", D("1000000"))
    ex.tick()
    app = create_app(ex, run_loop=run_loop, **kw)
    return TestClient(app), app, ex


class Holder:
    """Holds a lock on another thread until released: a stuck request."""

    def __init__(self, lock):
        self.lock = lock
        self.taken, self.done = threading.Event(), threading.Event()

    def __enter__(self):
        def hold():
            with self.lock.hold(5, label="stuck request"):
                self.taken.set()
                self.done.wait(30)
        self.thread = threading.Thread(target=hold, daemon=True)
        self.thread.start()
        assert self.taken.wait(5)
        return self

    def __exit__(self, *exc):
        self.done.set()
        self.thread.join(5)


# ---- backup (PR 19) x thread safety (PR 29) ------------------------------------------------------------

def test_data_routes_answer_busy_and_never_block_the_event_loop_while_the_lock_is_stuck(monkeypatch, tmp_path):
    """The backup routes are @unlocked (they do file I/O) and take ctx.locked() themselves. A stuck lock must
    give 503 busy, not a hang, and the async upload routes must not wait for the lock on the event loop."""
    monkeypatch.setattr(api_locking, "ROUTE_WAIT", 0.05)
    c, app, ex = make_app(tmp_path)
    ctx = app.state.ctx
    aid = list(ex.accounts)[0]
    name = c.post("/api/data/backups").json()["name"]
    token = c.post("/api/data/restore/preview", json={"backup": name}).json()["token"]
    exported = c.get(f"/api/data/accounts/{aid}/export").content
    before = json.dumps([a.to_plain() for a in ex.accounts.values()], sort_keys=True)
    with Holder(ctx.lock):
        calls = [("POST", "/api/data/backups", {}), ("POST", "/api/data/restore/preview", {"json": {"backup": name}}),
                 ("POST", "/api/data/restore/apply", {"json": {"token": token}}), ("GET", "/api/data/export", {}),
                 ("GET", f"/api/data/accounts/{aid}/export", {}),
                 ("POST", "/api/data/accounts/import/preview",
                  {"content": exported, "headers": {"content-type": "application/json"}})]
        for method, url, kw in calls:
            r = c.request(method, url, **kw)
            assert r.status_code == 503 and r.json()["busy"] is True, f"{method} {url}: {r.status_code} {r.text[:200]}"
        # the event loop stays free while an upload route waits for the lock in its worker thread
        monkeypatch.setattr(api_locking, "ROUTE_WAIT", 1.5)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", c.post("/api/data/restore/apply", json={"token": token})))
        t.start()
        time.sleep(0.2)
        t0 = time.monotonic()
        assert c.get("/api/auth/state").status_code == 200
        assert time.monotonic() - t0 < 1.0, "the event loop waited for the exchange lock"
        t.join(5)
        assert out["r"].status_code == 503
    assert json.dumps([a.to_plain() for a in ex.accounts.values()], sort_keys=True) == before
    # and once the lock is free the same token restores
    assert c.post("/api/data/restore/apply", json={"token": token}).status_code == 200
