"""Hardening that PR 30 left out: stream message checks and connection caps, isolation of an account that
cannot be valued, research type validation and rate limit, data-directory permissions, a GET-parameter fuzz
and the tick loop's stall during the heaviest allowed requests."""
import json
import os
import random
import threading
import time
import urllib.parse
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api import hardening
from app.api.server import create_app
from app.exchange import Exchange
from app.ledger.account import Account
from app.marketdata.research import ResearchHub, clean_types
from app.marketdata.synthetic import SyntheticFeed

D = Decimal


def client(run_loop=False, **kw):
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()))
    ex.create_account("Margin", "margin")
    ex.tick()
    return TestClient(create_app(ex, run_loop=run_loop, **kw), raise_server_exceptions=False), ex


def wait_for(cond, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.005)
    return cond()


def _break(ex) -> str:
    """Plant an account the way an old state file could: a starting cash no request can create any more."""
    a = Account.from_plain(ex.create_account("Broken", "margin").to_plain())
    a.starting_cash = D("1e999999")
    a.ledger.post(0, "deposit", "bad", [("cash", D("1e999999")), ("equity", D("-1e999999"))])
    ex.accounts[a.id] = a
    return a.id


# ---- an account that cannot be valued -----------------------------------------------------------------------

def test_one_broken_account_does_not_take_down_the_list_or_the_tick():
    c, ex = client()
    good = next(iter(ex.accounts))
    bad = _break(ex)
    rows = {r["id"]: r for r in c.get("/api/accounts").json()}
    assert rows[bad]["broken"] is True and "Reset it" in rows[bad]["error"]
    assert "broken" not in rows[good] and rows[good]["equity"] > 0
    ex._last_equity_sample = -1e9
    n = len(ex.accounts[good].equity_history)
    ex.tick()  # the equity sample, margin checks and accruals isolate the bad account
    assert len(ex.accounts[good].equity_history) == n + 1 and bad in ex.faults
    assert c.post(f"/api/accounts/{bad}/reset", json={}).status_code == 400  # it would keep the impossible cash
    assert c.post(f"/api/accounts/{bad}/reset", json={"starting_cash": "5000"}).status_code == 200
    assert "broken" not in {r["id"]: r for r in c.get("/api/accounts").json()}[bad] and bad not in ex.faults
    bad2 = _break(ex)
    assert c.delete(f"/api/accounts/{bad2}").status_code == 200 and bad2 not in ex.faults


def test_the_stream_keeps_running_for_others_when_one_account_is_broken():
    c, ex = client(run_loop=True, tick_seconds=0.01)
    good = next(iter(ex.accounts))
    bad = _break(ex)
    with c, c.websocket_connect("/ws") as g, c.websocket_connect("/ws") as b:
        g.send_json({"symbols": ["NVLX"], "account": good})
        b.send_json({"symbols": ["NVLX"], "account": bad})
        got_good = got_bad = None
        for _ in range(100):
            m = g.receive_json()
            if m.get("account", {}).get("id") == good:
                got_good = m
                break
        for _ in range(100):
            m = b.receive_json()
            if m.get("account", {}).get("id") == bad:
                got_bad = m
                break
    assert got_good and "NVLX" in got_good["quotes"] and not got_good["account"].get("broken")
    assert got_bad and got_bad["account"]["broken"] is True and "positions" not in got_bad


# ---- WebSocket messages and connections ---------------------------------------------------------------------

@pytest.mark.parametrize("payload,code", [("x" * (hardening.MAX_WS_MESSAGE + 1), hardening.WS_CLOSE_TOO_BIG),
                                          ("not json", 1008), ("[1, 2]", 1008), ('"text"', 1008)])
def test_a_bad_stream_message_closes_that_stream_with_a_code(payload, code):
    c, ex = client()
    with c.websocket_connect("/ws") as ws:
        ws.send_text(payload)
        msg = ws.receive()
        assert msg["type"] == "websocket.close" and msg["code"] == code


def test_binary_frames_close_the_stream():
    c, ex = client()
    with c.websocket_connect("/ws") as ws:
        ws.send_bytes(b"\x00\x01")
        assert ws.receive()["code"] == 1003


def test_stream_connections_are_capped_per_address_and_in_total(monkeypatch):
    monkeypatch.setattr(hardening, "MAX_WS_PER_ADDRESS", 2)
    c, ex = client()
    with c.websocket_connect("/ws"), c.websocket_connect("/ws"):
        with pytest.raises(Exception):
            with c.websocket_connect("/ws") as third:
                third.receive()
    monkeypatch.setattr(hardening, "MAX_WS_PER_ADDRESS", 100)
    monkeypatch.setattr(hardening, "MAX_WS_CONNECTIONS", 1)
    with c.websocket_connect("/ws"):
        with pytest.raises(Exception):
            with c.websocket_connect("/ws") as second:
                second.receive()


def test_stream_fields_stay_tolerant_and_bounded():
    sub = {"symbols": [], "account": None}
    hardening.ws_update(json.dumps({"symbols": [f"S{i}" for i in range(1000)], "account": "A" * 100}), sub)
    assert len(sub["symbols"]) == hardening.MAX_WS_SYMBOLS and sub["account"] is None
    hardening.ws_update(json.dumps({"symbols": "not a list", "account": "A0001"}), sub)
    assert len(sub["symbols"]) == hardening.MAX_WS_SYMBOLS and sub["account"] == "A0001"


def test_a_slow_stream_client_is_dropped(monkeypatch):
    monkeypatch.setattr(hardening, "WS_SEND_TIMEOUT", 0.01)
    import anyio

    class Slow:
        closed = None

        async def send_json(self, msg):
            await anyio.sleep(1)

        async def close(self, code):
            Slow.closed = code
    c, ex = client(run_loop=True, tick_seconds=0.01)
    with c:
        slow = Slow()
        c.app.state.ctx.clients[slow] = {"symbols": [], "account": None, "addr": "x"}
        assert wait_for(lambda: slow not in c.app.state.ctx.clients)
    assert Slow.closed == hardening.WS_CLOSE_POLICY


# ---- research ------------------------------------------------------------------------------------------------

def test_corporate_action_types_are_validated_and_canonical():
    assert clean_types("forward_split, cash_dividend,cash_dividend") == "cash_dividend,forward_split"
    assert clean_types(None) is None and clean_types(" ") is None
    with pytest.raises(ValueError, match="unknown corporate action type"):
        clean_types("x" * 1000)


class _FakeAlpaca:
    def __init__(self):
        self.calls = 0

    def corporate_actions(self, symbol, start, end, types, max_wait=5):
        self.calls += 1
        return {}

    def news(self, symbol, limit, max_wait=5):
        self.calls += 1
        return []


def test_provider_backed_research_is_rate_limited_per_client():
    c, ex = client()
    fake = _FakeAlpaca()
    ex.feed.research = ResearchHub(alpaca=fake)
    assert c.get("/api/research/corporate-actions/NVLX", params={"types": "bogus"}).status_code == 400
    for t in ("cash_dividend", "cash_dividend,cash_dividend", " cash_dividend"):
        assert c.get("/api/research/corporate-actions/NVLX", params={"types": t}).status_code == 200
    assert fake.calls == 1  # equal requests share one cache entry
    codes = [c.get("/api/research/news/NVLX").status_code for _ in range(hardening.RESEARCH_RATE_BURST + 5)]
    assert 429 in codes
    r = c.get("/api/research/news/NVLX")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert c.get("/api/research/sources").status_code == 200  # not provider-backed, never limited


def test_simulated_research_is_not_rate_limited():
    c, ex = client()
    assert all(c.get("/api/research/news/NVLX").status_code == 200 for _ in range(hardening.RESEARCH_RATE_BURST + 5))


# ---- file permissions --------------------------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_data_directory_is_private_and_loose_files_are_reported(tmp_path):
    d = tmp_path / "data"
    hardening.private_dir(str(d))
    assert (os.stat(d).st_mode & 0o777) == 0o700
    env = tmp_path / ".env"
    env.write_text("PAPERDESK_PASSCODE=x\n")
    os.chmod(env, 0o644)
    warnings = hardening.permission_warnings([str(d), str(env), str(tmp_path / "missing")])
    assert len(warnings) == 1 and "chmod 600" in warnings[0]


# ---- GET parameters fuzz -------------------------------------------------------------------------------------------

NASTY = [None, True, 0, -1, 10 ** 30, 1e308, 0.1 + 0.2, "", " ", "-0", "1e999999", "1e-999999", "NaN", "Infinity",
         "abc", "x" * 3000, "\u0000", "💥" * 50, "‮", "../../etc/passwd", "<script>", "javascript:alert(1)",
         "[1,2]", "{}", "A0001", "A9999", "NVLX", "NVLX261218C00150000", "2026-12-31", "1999-01-01", "9999-99-99",
         "1d", "1m", "live", "replay:x", "cash_dividend", "open", "all"]


def test_every_get_route_survives_junk_parameters_and_the_ledgers_still_balance():
    rng = random.Random(20261127)
    c, ex = client()
    aid = next(iter(ex.accounts))
    c.post(f"/api/accounts/{aid}/orders", json={"symbol": "NVLX", "side": "buy", "qty": 2})
    ids = {"aid": aid, "sym": "NVLX", "symbol": "NVLX", "kind": "ledger.csv", "month": "2026-11", "name": "Main"}
    routes = [r for r in c.app.routes if isinstance(r, APIRoute) and r.path.startswith("/api") and "GET" in r.methods]
    assert len(routes) >= 30
    for route in routes:
        names = [p.alias for p in route.dependant.query_params]
        for _ in range(10):
            path = route.path
            for k, real in ids.items():
                junk = urllib.parse.quote(str(rng.choice(NASTY))[:200], safe="") or "x"
                path = path.replace("{" + k + "}", real if rng.random() < 0.7 else junk)
            params = {n: str(rng.choice(NASTY))[:3000] for n in names if rng.random() < 0.8}
            params["unknown"] = "x"
            t = time.monotonic()
            r = c.get(path, params=params)
            assert r.status_code < 500, (route.path, params, r.status_code, r.text[:300])
            assert len(r.content) <= 4 * 1024 * 1024, (route.path, len(r.content))
            assert time.monotonic() - t < 3, route.path
    for a in ex.accounts.values():
        assert all(sum(v for _, v in t.postings) == 0 for t in a.ledger.transactions)
        assert {k: v for k, v in a.ledger.recompute().items() if v} == {k: v for k, v in a.ledger.balances.items() if v}
    r = c.get("/api/accounts")
    assert r.status_code == 200 and not any(a.get("broken") for a in r.json())


# ---- tick-loop stall -------------------------------------------------------------------------------------------------

def test_tick_loop_stall_during_the_heaviest_allowed_requests():
    """The heaviest requests the limits allow must not stall the tick for long. Before the number bounds, one CSV
    export of an account with starting cash 1e-150000000 stalled it for about 2 s (and the review measured 12.9 s)."""
    from app.analytics import journal
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()))
    aid = ex.create_account("Margin", "margin", D("1000000000")).id
    ex.tick()
    a = ex.accounts[aid]
    for i in range(2000):  # a large journal: 2,000 notes at the longest allowed length
        a.journal.append({"id": f"J{i + 1}", "target_type": "order", "target_id": "O1", "note": "n" * journal.MAX_NOTE,
                          "tags": ["t" * 32] * 12, "created_at": float(i), "updated_at": float(i)})
    ticks = []
    real_tick = ex.tick

    def counted():
        ticks.append(time.monotonic())
        return real_tick()
    ex.tick = counted
    with TestClient(create_app(ex, run_loop=True, tick_seconds=0.02), raise_server_exceptions=False) as c:
        assert wait_for(lambda: len(ticks) >= 5)
        big = json.dumps({"symbol": "NVLX", "side": "buy", "qty": 1, "pad": "x" * 60_000})
        heavy = [("GET", f"/api/accounts/{aid}/export/journal.csv", {}), ("GET", f"/api/accounts/{aid}/journal", {}),
                 ("GET", "/api/bars/NVLX", {"params": {"tf": "1m", "limit": 5000}}),
                 ("POST", f"/api/accounts/{aid}/orders/preview", {"content": big, "headers": {"content-type": "application/json"}}),
                 ("POST", "/api/accounts", {"json": {"starting_cash": "1e-150000000"}}),
                 ("GET", f"/api/accounts/{aid}/export/ledger.csv", {}), ("GET", f"/api/accounts/{aid}/analytics", {})]
        errors = []
        start = time.monotonic()

        def run():
            try:
                for _ in range(2):
                    for method, path, kw in heavy:
                        assert c.request(method, path, **kw).status_code < 500, path
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
