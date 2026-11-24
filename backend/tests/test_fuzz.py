"""Adversarial input against every /api route, and the tick loop's stall during the heaviest allowed requests.

The fuzz sends wrong types, huge and tiny numbers, long strings, deep nesting, unicode, missing and extra fields
to every non-GET route, and junk to every GET query and path parameter. Nothing may answer 5xx, answers must be
bounded in size and time, and afterwards every ledger still balances and /api/accounts still works.
"""
import json
import random
import threading
import time
import urllib.parse
from decimal import Decimal

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.analytics import journal
from app.api.server import create_app
from app.core import limits
from app.exchange import Exchange
from app.lifecycle import settings as cfg
from app.marketdata.synthetic import SyntheticFeed

MAX_ANSWER = 4 * 1024 * 1024
MAX_SECONDS = 3.0

NASTY = [None, True, False, 0, -1, 1, 2, 10 ** 30, -10 ** 30, 1e308, -1e-308, 0.1 + 0.2, 123.456789, "", " ", "0", "-0",
         "1", "1e999999", "1e-999999", "1e-150000000", "NaN", "-Infinity", "abc", "x" * 5000, "\u0000", "💥" * 50,
         "‮", "../../etc/passwd", "<script>alert(1)</script>", "javascript:alert(1)", [], [1, 2], ["NVLX"] * 50, {},
         {"a": 1}, {"$gt": ""}, "A0001", "A9999", "NVLX", "nvlx", "NVLX261218C00150000", "ZZZZ999999C99999999", "buy",
         "sell", "market", "limit", "stop", "trailing_stop", "gtc", "gtd", "day", "ioc", "2026-12-31", "1999-01-01",
         "credit", "debit", "fifo", "live", "replay:x", "sample-2025-11-28"]
KEYS = ["symbol", "side", "qty", "type", "tif", "limit_price", "stop_price", "trail_amount", "trail_percent", "take_profit",
        "stop_loss", "legs", "oco", "triggers", "triggers_oco", "starting_cash", "name", "price", "op", "note", "tags",
        "target_type", "target_id", "method", "settings", "session", "speed", "seconds", "time", "offset", "updated",
        "data", "passcode", "account_type", "good_till_date", "extended_hours", "client_id", "price_effect", "ratio",
        "play"] + list(cfg.SPEC)


def deep(n: int):
    v = []
    for _ in range(n):
        v = [v]
    return v


def bodies(rng: random.Random, templates: list) -> list:
    out = [None, [], "text", 5, deep(2000), {"x": deep(500)}, {k: None for k in KEYS}, {"a" * 1000: 1},
           {k: "1e999999" for k in KEYS}, {k: [] for k in KEYS}, {k: {"a": {"b": 1}} for k in KEYS}]
    for _ in range(25):
        out.append({rng.choice(KEYS): rng.choice(NASTY) for _ in range(rng.randint(0, 6))})
    for t in templates:
        out.append(dict(t))
        for k in list(t):
            for v in rng.sample(NASTY, 6):
                out.append(dict(t, **{k: v}))
            out.append({kk: vv for kk, vv in t.items() if kk != k})  # a missing field
        out.append(dict(t, extra=rng.choice(NASTY), **{"legs": [dict(t)] * 3}))
    return out


def fill(path: str, ids: dict, rng: random.Random) -> str:
    for name, real in ids.items():
        if "{" + name + "}" in path:
            junk = urllib.parse.quote(str(rng.choice(NASTY))[:200], safe="") or "x"
            path = path.replace("{" + name + "}", real if rng.random() < 0.7 else junk)
    return path


def balanced(ex) -> None:
    for a in ex.accounts.values():
        assert all(sum(v for _, v in t.postings) == 0 for t in a.ledger.transactions)
        assert {k: v for k, v in a.ledger.recompute().items() if v} == {k: v for k, v in a.ledger.balances.items() if v}
        assert sum(a.ledger.balances.values()) == 0


def request(c, method, path, **kw):
    t = time.monotonic()
    r = c.request(method, path, **kw)
    took = time.monotonic() - t
    assert r.status_code < 500, (method, path, kw.get("json") if "json" in kw else kw.get("params"), r.status_code, r.text[:300])
    assert len(r.content) <= MAX_ANSWER, (method, path, len(r.content))
    assert took < MAX_SECONDS, (method, path, took)
    return r


def test_fuzz_every_route():
    rng = random.Random(20261124)
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()))
    aid = ex.create_account("Margin", "margin").id
    ex.create_account("Cash", "cash")
    ex.tick()
    c = TestClient(create_app(ex, run_loop=False), raise_server_exceptions=False)
    px = ex.quote("NVLX").last
    low = str((px * Decimal("0.5")).quantize(Decimal("0.01")))
    oid = c.post(f"/api/accounts/{aid}/orders", json={"symbol": "NVLX", "side": "buy", "qty": 1, "type": "limit",
                                                      "limit_price": low, "tif": "gtc"}).json()[0]["id"]
    c.post(f"/api/accounts/{aid}/orders", json={"symbol": "NVLX", "side": "buy", "qty": 2})
    jid = c.post(f"/api/accounts/{aid}/journal", json={"target_type": "order", "target_id": oid, "note": "n"}).json()["id"]
    alert = c.post("/api/alerts", json={"symbol": "NVLX", "op": "above", "price": "1"}).json()["id"]
    exp = c.get("/api/options/expirations/NVLX").json()[0]["date"]
    chain = c.get("/api/options/chain/NVLX", params={"expiry": exp}).json()["rows"]
    call = chain[len(chain) // 2]["call"]["symbol"]
    templates = [
        {"symbol": "NVLX", "side": "buy", "qty": 1, "type": "limit", "limit_price": low, "tif": "gtc"},
        {"symbol": "NVLX", "side": "sell", "qty": 1, "type": "trailing_stop", "trail_percent": "5", "tif": "gtc"},
        {"symbol": call, "side": "buy", "qty": 1, "type": "market"},
        {"legs": [{"symbol": call, "side": "buy"}, {"symbol": "NVLX", "side": "sell", "ratio": 100}], "type": "limit",
         "limit_price": "1.00", "price_effect": "debit"},
        {"symbol": "NVLX", "side": "buy", "qty": 1, "oco": [{"symbol": "NVLX", "side": "buy", "qty": 1, "type": "limit",
                                                              "limit_price": low}] * 2},
        {"starting_cash": "5000", "name": "x", "type": "cash"},
        {"symbol": "NVLX", "op": "above", "price": "10"},
        {"target_type": "order", "target_id": oid, "note": "n", "tags": ["a"]},
        {"method": "lifo"}, {"settings": {"margin_interest_rate": "5"}}, {"speed": 2}, {"seconds": 60},
        {"updated": 1, "data": [1]},
    ]
    payloads = bodies(rng, templates)
    ids = {"aid": aid, "oid": oid, "jid": jid, "alert_id": alert, "name": "Main", "sym": "NVLX", "symbol": "NVLX",
           "kind": "ledger.csv", "month": "2026-11", "tid": "x"}
    routes = [r for r in c.app.routes if isinstance(r, APIRoute) and r.path.startswith("/api")]
    mutating = [(m, r) for r in routes for m in r.methods if m not in ("GET", "HEAD")]
    assert len(mutating) >= 30
    for method, route in mutating:
        for body in rng.sample(payloads, min(len(payloads), 60)):
            path = fill(route.path, ids, rng)
            if body is None:
                request(c, method, path)
            else:
                request(c, method, path, content=json.dumps(body), headers={"content-type": "application/json"})
        request(c, method, fill(route.path, ids, rng), content=b"{not json", headers={"content-type": "application/json"})
        request(c, method, fill(route.path, ids, rng), content=b"\xff\xfe\x00", headers={"content-type": "application/json"})
    for route in routes:
        if "GET" not in route.methods:
            continue
        names = [p.alias for p in route.dependant.query_params]
        for _ in range(12):
            params = {n: str(rng.choice(NASTY))[:3000] for n in names if rng.random() < 0.8}
            params["unknown"] = "x"
            request(c, "GET", fill(route.path, ids, rng), params=params)
    if c.app.state.ctx.replay is not None:
        c.post("/api/replay/exit")
    ex = c.app.state.ctx.ex
    balanced(ex)
    r = c.get("/api/accounts")
    assert r.status_code == 200 and r.json() and not any(a.get("broken") for a in r.json())
    ex.tick()
    assert len(ex.accounts) <= limits.MAX_ACCOUNTS


def test_tick_loop_stall_during_the_heaviest_allowed_requests():
    """The heaviest requests the limits allow (the largest journal export and listing, long bar series, a full-size
    body) must not stall the tick loop for long. Before the number bounds, one CSV export stalled it for 12.9 s."""
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()))
    aid = ex.create_account("Margin", "margin", Decimal("1000000000")).id
    ex.tick()
    a = ex.accounts[aid]
    for i in range(limits.MAX_JOURNAL_ENTRIES):  # the largest journal an account may hold
        a.journal.append({"id": f"J{i + 1}", "target_type": "order", "target_id": "O1", "note": "n" * journal.MAX_NOTE,
                          "tags": ["t" * 32] * 12, "created_at": float(i), "updated_at": float(i)})
    ticks = []
    real_tick = ex.tick

    def counted():
        ticks.append(time.monotonic())
        return real_tick()
    ex.tick = counted
    app = create_app(ex, run_loop=True, tick_seconds=0.02)
    with TestClient(app, raise_server_exceptions=False) as c:
        deadline = time.monotonic() + 5
        while len(ticks) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        start = time.monotonic()
        heavy = [("GET", f"/api/accounts/{aid}/export/journal.csv", {}), ("GET", f"/api/accounts/{aid}/journal", {}),
                 ("GET", "/api/bars/NVLX", {"params": {"tf": "1m", "limit": 5000}}),
                 ("POST", f"/api/accounts/{aid}/orders/preview",
                  {"content": json.dumps({"symbol": "NVLX", "side": "buy", "qty": 1, "pad": "x" * (limits.MAX_BODY_BYTES - 200)}),
                   "headers": {"content-type": "application/json"}}),
                 ("GET", f"/api/accounts/{aid}/export/ledger.csv", {}), ("GET", f"/api/accounts/{aid}/analytics", {})]
        errors = []

        def run():
            try:
                for _ in range(2):
                    for method, path, kw in heavy:
                        assert c.request(method, path, **kw).status_code < 500
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        workers = [threading.Thread(target=run) for _ in range(2)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(60)
        end = time.monotonic()
    assert not errors, errors
    during = [t for t in ticks if start <= t <= end + 0.5]
    gaps = [b - a for a, b in zip(during, during[1:])]
    assert during and max(gaps or [0]) < 1.5, f"tick loop stalled for {max(gaps):.2f} s"
