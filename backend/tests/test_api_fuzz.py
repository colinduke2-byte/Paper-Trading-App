"""Every non-GET /api route answers wrong types and malformed numbers with a 4xx, never a 5xx.

The bodies are built from a valid baseline per route with one field replaced by a bad value, plus
whole-body replacements (wrong JSON type, invalid JSON). A route added later without a baseline here
still gets the whole-body checks, and the test fails if a route is missing from ROUTES so it is
considered on purpose.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.exchange import Exchange
from app.lifecycle import settings as cfg
from app.marketdata.replay.samples import SAMPLES
from app.marketdata.synthetic import SyntheticFeed

BAD = ["abc", "1,000", "1,0,0", "", "  ", [], [1], {}, {"a": 1}, True, None, "NaN", "Infinity", "-Infinity",
       "1e999999", -1, 0, 10 ** 30, "1e-999999", 1.5, "0x10", "١٢"]
RAW_BAD = [b"{", b"[1,", b"\xff\xfe", b"1e999", b'{"qty": 1e999, "starting_cash": 1e999, "seconds": 1e999}',
           b'{"time": 1e999}', b'{"offset": 1e999}', b'{"speed": 1e999}', b'{"price": -1e999}']
WHOLE = [[], "text", 5, None, True, {}, {"x": 1}]

ORDER = {"symbol": "NVLX", "side": "buy", "qty": "1", "type": "limit", "limit_price": "1.00", "tif": "day",
         "stop_price": None, "trail_amount": None, "trail_percent": None, "take_profit": None, "stop_loss": None,
         "good_till_date": None, "extended_hours": False, "client_id": None, "oco": None, "triggers": None,
         "legs": None, "price_effect": None}


@pytest.fixture(scope="module")
def setup():
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()))
    ex.create_account("Margin", "margin")
    ex.create_account("Cash", "cash", 25000)
    ex.tick()
    app = create_app(ex, run_loop=False)
    c = TestClient(app, raise_server_exceptions=False)
    aid = next(iter(ex.accounts))
    exp = c.get("/api/options/expirations/NVLX").json()[0]["date"]
    rows = c.get("/api/options/chain/NVLX", params={"expiry": exp}).json()["rows"]
    calls = [r["call"]["symbol"] for r in rows[:2]]
    return c, app, ex, aid, calls


def working_order(c, aid):
    r = c.post(f"/api/accounts/{aid}/orders", json=dict(ORDER, limit_price="1.00"))
    assert r.status_code == 200, r.text
    return r.json()[0]["id"]


def journal_entry(c, aid, oid):
    r = c.post(f"/api/accounts/{aid}/journal", json={"target_type": "order", "target_id": oid, "note": "n"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def baselines(aid, oid, jid, calls):
    legs = [{"symbol": calls[0], "side": "buy", "ratio": 1}, {"symbol": calls[1], "side": "sell", "ratio": 1}]
    multi = {"legs": legs, "qty": 1, "type": "limit", "limit_price": "1.00", "price_effect": "debit", "tif": "day"}
    settings = {k: v for k, v in cfg.defaults().items()}
    return {
        ("POST", "/api/accounts"): {"name": "X", "type": "margin", "starting_cash": "5000"},
        ("POST", f"/api/accounts/{aid}/reset"): {"starting_cash": "100000"},
        ("DELETE", f"/api/accounts/{aid}"): None,
        ("POST", f"/api/accounts/{aid}/orders/preview"): ORDER,
        ("POST", f"/api/accounts/{aid}/orders"): ORDER,
        ("DELETE", f"/api/accounts/{aid}/orders"): None,
        ("DELETE", f"/api/accounts/{aid}/orders/{oid}"): None,
        ("PATCH", f"/api/accounts/{aid}/orders/{oid}"): {"qty": "1", "limit_price": "1.01", "stop_price": None},
        ("PUT", "/api/watchlists/Fuzz"): ["NVLX"],
        ("DELETE", "/api/watchlists/Fuzz"): None,
        ("POST", "/api/alerts"): {"symbol": "NVLX", "op": "above", "price": "10"},
        ("DELETE", "/api/alerts/AL99999"): None,
        ("POST", "/api/auth/login"): {"passcode": "x"},
        ("POST", "/api/auth/logout"): None,
        ("POST", f"/api/accounts/{aid}/multileg/preview"): multi,
        ("POST", f"/api/accounts/{aid}/multileg"): multi,
        ("POST", "/api/replay/start"): {"session": SAMPLES[0][0], "starting_cash": "10000", "account_type": "margin",
                                        "speed": 1, "play": False},
        ("POST", "/api/replay/pause"): None,
        ("POST", "/api/replay/resume"): None,
        ("POST", "/api/replay/speed"): {"speed": 2},
        ("POST", "/api/replay/step"): {"seconds": 5},
        ("POST", "/api/replay/seek"): {"offset": 5, "time": None},
        ("POST", "/api/replay/exit"): None,
        ("PUT", f"/api/accounts/{aid}/lot-method"): {"method": "fifo"},
        ("POST", f"/api/accounts/{aid}/journal"): {"target_type": "order", "target_id": oid, "note": "n", "tags": ["a"]},
        ("PATCH", f"/api/accounts/{aid}/journal/{jid}"): {"note": "m", "tags": ["b"]},
        ("DELETE", f"/api/accounts/{aid}/journal/{jid}"): None,
        ("PUT", f"/api/accounts/{aid}/settings"): {"settings": settings},
        ("POST", f"/api/accounts/{aid}/settings/reset"): None,
        ("PUT", "/api/chartstate/settings"): {"updated": 1, "data": {"type": "candles"}},
        ("PUT", "/api/chartstate/drawings/NVLX"): {"updated": 1, "data": [{"id": "d1"}]},
        ("DELETE", "/api/chartstate"): None,
        ("POST", "/api/alert-rules"): [
            {"kind": "price", "symbol": "NVLX", "op": "above", "price": "10", "account_id": aid, "repeat": False,
             "cooldown": 60, "expires_at": None, "expires_in_hours": None, "message": "m"},
            {"kind": "change", "symbol": "NVLX", "op": "above", "value": "5", "basis": "reference", "reference": "10"},
            {"kind": "volume", "symbol": "NVLX", "value": "1000", "mode": "level"},
            {"kind": "indicator", "symbol": "NVLX", "indicator": "rsi", "tf": "1d", "period": 14, "op": "above", "value": "70"},
            {"kind": "option", "symbol": calls[0], "op": "above", "metric": "delta", "value": "0.5"},
            {"kind": "position", "account_id": aid, "symbol": "NVLX", "op": "below", "metric": "unrealized_pnl", "value": "-5"},
            {"kind": "account", "account_id": aid, "op": "below", "metric": "equity", "value": "5"},
            {"kind": "reminder", "in_minutes": "5", "at": None, "message": "m", "repeat": True, "cooldown": 120},
            {"kind": "calendar", "account_id": aid, "events": ["dividend"], "days_before": 3},
        ],
        ("PATCH", "/api/alert-rules/ALID"): {"price": "11", "enabled": False, "message": "x", "cooldown": 60},
        ("DELETE", "/api/alert-rules/ALID"): None,
        ("POST", "/api/notifications/read"): {"account_id": aid, "ids": ["n1"], "read": True},
        ("DELETE", "/api/notifications"): None,
        # data and backups (routes_data.py); this exchange has no data folder, so most answer 409
        ("POST", "/api/data/backups"): None,
        ("DELETE", "/api/data/backups/BKNAME"): None,
        ("POST", "/api/data/restore/preview"): {"backup": "paperdesk-manual-20261124T000000Z.zip"},
        ("POST", "/api/data/restore/apply"): {"token": "t"},
        ("POST", "/api/data/accounts/import/preview"): {"format": "paperdesk-account", "format_version": 1},
        ("POST", "/api/data/accounts/import/apply"): {"token": "t", "name": "n"},
        ("POST", "/api/data/recovery/restore"): {"backup": "paperdesk-manual-20261124T000000Z.zip"},
        ("POST", "/api/data/recovery/fresh"): None,
        ("POST", "/api/data/recovery/quit"): None,
        # discovery (routes_discovery.py, PR 20)
        ("POST", "/api/watchlist-sets"): {"name": "Fuzz", "symbols": ["NVLX"], "layout": {}},
        ("PATCH", "/api/watchlist-sets/Fuzz"): {"name": "Fuzz", "symbols": ["NVLX"], "columns": ["last"]},
        ("POST", "/api/watchlist-sets/Fuzz/duplicate"): {"name": "Fuzz copy"},
        ("DELETE", "/api/watchlist-sets/Fuzz"): None,
        ("PUT", "/api/watchlist-order"): ["Main"],
        ("POST", "/api/watchlist-sets/Fuzz/symbols"): {"symbols": ["NVLX"], "at": "end"},
        ("POST", "/api/search/recent"): {"symbol": "NVLX"},
        ("POST", "/api/screener/run"): {"filters": [{"field": "last", "min": 1}], "sort": "symbol", "desc": False, "limit": 10},
        ("POST", "/api/screens"): {"name": "S", "filters": [], "sort": None, "desc": True, "columns": []},
        ("PUT", "/api/screens/SCID"): {"name": "S", "filters": [], "sort": None, "desc": True, "columns": []},
        ("DELETE", "/api/screens/SCID"): None,
    }


def _template(path, aid, oid, jid):
    return path.replace(aid, "{aid}").replace(oid, "{oid}").replace(jid, "{jid}").replace("/Fuzz", "/{name}") \
        .replace("/AL99999", "/{alert_id}").replace("/ALID", "/{alert_id}").replace("/drawings/NVLX", "/drawings/{sym:path}") \
        .replace("/backups/BKNAME", "/backups/{name}").replace("/screens/SCID", "/screens/{sid}")


def variants(body):
    """One field at a time replaced by each bad value (recursing one level into lists of objects)."""
    if isinstance(body, dict):
        for k, v in body.items():
            for bad in BAD:
                yield dict(body, **{k: bad})
            if isinstance(v, dict):
                for inner in variants(v):
                    yield dict(body, **{k: inner})
            if isinstance(v, list) and v and isinstance(v[0], dict):
                for inner in variants(v[0]):
                    yield dict(body, **{k: [inner] + v[1:]})
    elif isinstance(body, list):
        for bad in BAD:
            yield [bad]
            yield body + [bad]


def check(c, method, path, **kw):
    r = c.request(method, path, **kw)
    assert r.status_code < 500, (method, path, kw, r.status_code, r.text[:300])
    if r.status_code >= 400 and not path.startswith("/api/auth"):
        assert r.headers["content-type"].startswith("application/json"), (method, path, r.text[:200])
        assert isinstance(r.json().get("detail"), (str, list)), r.text
        assert "<class" not in r.text and "decimal." not in r.text, r.text
    return r


def test_every_mutating_route_is_covered(setup):
    c, app, ex, aid, calls = setup
    oid = working_order(c, aid)
    jid = journal_entry(c, aid, oid)
    covered = {(m, _template(p, aid, oid, jid)) for m, p in baselines(aid, oid, jid, calls)}
    routes = {(m, r.path) for r in app.routes for m in (getattr(r, "methods", None) or ()) if m not in ("GET", "HEAD")}
    assert routes - covered == set(), "add a baseline body for the new route to tests/test_api_fuzz.py"


def test_no_route_answers_bad_input_with_a_server_error(setup):
    c, app, ex, aid, calls = setup
    ctx = app.state.ctx
    for (method, path), base in baselines(aid, "OID", "JID", calls).items():
        if method == "DELETE" and path == f"/api/accounts/{aid}":
            continue  # deleting the account would end the run; it takes no body
        # the replay sandbox swaps the exchange: its controls and the order routes are fuzzed there too
        states = (False, True) if "/replay/" in path or path.endswith("/orders") else (False,)
        for replay_active in states:
            if replay_active and ctx.replay is None:
                assert c.post("/api/replay/start", json={"session": SAMPLES[0][0]}).status_code == 200
            if not replay_active and ctx.replay is not None:
                c.post("/api/replay/exit")
            a = next(iter(ctx.ex.accounts))
            oid = working_order(c, a)
            jid = journal_entry(c, a, oid)
            r = c.post("/api/alert-rules", json={"kind": "price", "symbol": "NVLX", "op": "above", "price": "100000"})
            assert r.status_code == 200, r.text
            real = path.replace(aid, a).replace("OID", oid).replace("JID", jid).replace("ALID", r.json()["id"])
            bases = base if isinstance(base, list) and base and isinstance(base[0], dict) and "kind" in base[0] else [base]
            bodies = [v for b in bases if b is not None for v in variants(b)] + WHOLE
            base = bases[0]
            for body in bodies:
                check(c, method, real, json=body)
                if ctx.replay is not None and not replay_active:
                    c.post("/api/replay/exit")  # a variant that still starts a replay is fine; undo it
            for raw in RAW_BAD:
                check(c, method, real, content=raw, headers={"content-type": "application/json"})
                if ctx.replay is not None and not replay_active:
                    c.post("/api/replay/exit")
            # unknown ids are a 404 with a sentence, never a 500
            for bad_id in ("ZZZ", "%20", "A0001x"):
                check(c, method, real.replace(a, bad_id) if a in real else real, json=base)
    if ctx.replay is not None:
        c.post("/api/replay/exit")


def test_malformed_starting_cash_is_a_400_with_a_clear_message(setup):
    c, app, ex, aid, calls = setup
    for bad in ("1,000", "abc"):
        for path in ("/api/accounts", f"/api/accounts/{aid}/reset"):
            r = c.post(path, json={"name": "X", "starting_cash": bad})
            assert r.status_code == 400 and r.headers["content-type"].startswith("application/json"), r.text
            assert "starting cash is not a number" in r.json()["detail"]
    assert "without thousands separators" in c.post("/api/accounts", json={"starting_cash": "1,000"}).json()["detail"]
    r = c.post("/api/accounts", json={"starting_cash": [1]})
    assert r.status_code == 400 and r.json()["detail"] == "starting cash must be a number, not a list"


def test_only_deliberate_lookups_are_404(setup):
    c, app, ex, aid, calls = setup
    r = c.get("/api/accounts/NOPE")
    assert r.status_code == 404 and r.json()["detail"] == "no such account NOPE"
    r = c.patch(f"/api/accounts/{aid}/orders/NOPE", json={"qty": "1"})
    assert r.status_code == 404 and r.json()["detail"] == "no such order"


def test_order_numbers_name_the_field(setup):
    c, app, ex, aid, calls = setup
    r = c.post(f"/api/accounts/{aid}/orders", json=dict(ORDER, qty="abc"))
    assert r.status_code == 422, (r.text, app.state.ctx.mode)
    d = r.json()["detail"]
    assert "quantity is not a number" in d and "abc" in d and "class" not in d and "Conversion" not in d
    r = c.post(f"/api/accounts/{aid}/orders", json=dict(ORDER, limit_price="1,5"))
    assert "limit price is not a number" in r.json()["detail"]
    oid = working_order(c, aid)
    r = c.patch(f"/api/accounts/{aid}/orders/{oid}", json={"limit_price": "x"})
    assert r.status_code in (400, 422) and "limit price is not a number" in r.json()["detail"]


def test_raw_invalid_json_is_rejected(setup):
    c, *_ = setup
    r = c.post("/api/accounts", content=b"{", headers={"content-type": "application/json"})
    assert r.status_code == 422
    json.dumps(r.json())


def test_an_unexpected_error_is_a_json_500_without_internals(setup, monkeypatch):
    c, app, ex, aid, calls = setup

    def boom(*a, **k):
        raise TypeError("secret internals")
    monkeypatch.setattr(app.state.ctx.ex, "summary", boom)
    r = c.get(f"/api/accounts/{aid}")
    assert r.status_code == 500 and r.headers["content-type"].startswith("application/json")
    assert "internal error (TypeError)" in r.json()["detail"] and "secret" not in r.text
    # a stray KeyError is such a bug too, not a misleading 404
    monkeypatch.setattr(app.state.ctx.ex, "summary", lambda *a: {}["equity"])
    assert c.get(f"/api/accounts/{aid}").status_code == 500


def test_a_malformed_websocket_subscription_does_not_stop_the_stream():
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()))
    ex.create_account("Margin", "margin")
    ex.tick()
    c = TestClient(create_app(ex, run_loop=True, tick_seconds=0.02))
    with c:
        with c.websocket_connect("/ws") as bad, c.websocket_connect("/ws") as good:
            bad.send_json({"symbols": ["NVLX"], "account": ["A0001"]})
            good.send_json({"symbols": ["NVLX"], "account": "A0001"})
            for _ in range(50):
                msg = good.receive_json()
                if msg.get("account"):
                    break
            assert msg["account"]["id"] == "A0001"


def test_option_descriptions_match_the_ui():
    """frontend/src/util.test.ts checks describeOption() against these same strings."""
    from app.options.symbology import describe
    assert describe("MKTX261204C00400000") == "MKTX 04 Dec 2026 400 Call"
    assert describe("NVLX270115P00102500") == "NVLX 15 Jan 2027 102.5 Put"
