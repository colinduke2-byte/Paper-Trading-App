"""Security hardening from the whole-app review: one test (or more) per item."""
import gzip
import io
import json
import time
import urllib.request
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

import app.exchange as exchange_mod
from app.api import auth
from app.api.server import create_app
from app.core import money
from app.exchange import Exchange
from app.marketdata import finnhub, http, research
from app.marketdata.replay import format as fmt
from app.marketdata.replay.library import BUNDLED_DIR
from app.marketdata.synthetic import SyntheticFeed
from app.options import symbology

PASS = "correct horse 42"
SAMPLE = f"{BUNDLED_DIR}/sample-2025-11-28.pdsession.gz"


def client(data_dir=None, **kw):
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()), data_dir=data_dir)
    if not ex.load():
        ex.create_account("Margin", "margin")
    ex.tick()
    return TestClient(create_app(ex, run_loop=False), raise_server_exceptions=False, **kw), ex


# ---- money.parse magnitude and exponent bounds ------------------------------------------------------

@pytest.mark.parametrize("raw, why", [("1e999999999", "too large"), ("1e16", "too large"), (10 ** 16, "too large"),
                                      ("-1e20", "too large"), ("0E+999999999", "too large"),
                                      ("1e-999999999", "too many decimal places"),
                                      ("0E-999999999", "too many decimal places"),
                                      ("1" * 65, "too long")])
def test_parse_refuses_extreme_numbers(raw, why):
    with pytest.raises(ValueError, match=why) as e:
        money.parse(raw, "quantity")
    assert str(e.value).startswith("quantity ")


@pytest.mark.parametrize("raw", ["9999999999999999", "0.30000000000000004", 1e-7, "0", "-0.0001", 1e15, "123.4567"])
def test_parse_keeps_ordinary_numbers(raw):
    assert money.parse(raw) == Decimal(str(raw))


@settings(max_examples=300, deadline=None)
@given(st.one_of(st.decimals(allow_nan=False, allow_infinity=False), st.integers(), st.floats(allow_nan=False,
                                                                                             allow_infinity=False)))
def test_parse_result_is_always_bounded(x):
    try:
        d = money.parse(x)
    except ValueError:
        return
    assert abs(d) < Decimal(10) ** 16 and d.as_tuple().exponent >= money.MIN_EXPONENT
    money.cents(d * 1000)  # arithmetic on any accepted value stays inside the decimal context


def test_extreme_quantity_is_a_4xx_not_a_crash():
    c, ex = client()
    aid = next(iter(ex.accounts))
    t = time.monotonic()
    for q in ("1e999999999", "1e-999999999", "0E+999999999"):
        r = c.post(f"/api/accounts/{aid}/orders/preview",
                   json={"symbol": "NVLX", "side": "buy", "qty": q, "type": "market", "tif": "day"})
        assert r.status_code in (400, 422) and "quantity" in r.json()["detail"], r.text
    assert time.monotonic() - t < 5


# ---- reset validates before it replaces state ------------------------------------------------------

def test_bad_reset_leaves_the_account_and_its_orders_alone(tmp_path):
    c, ex = client(str(tmp_path))
    aid = next(iter(ex.accounts))
    r = c.post(f"/api/accounts/{aid}/orders", json={"symbol": "NVLX", "side": "buy", "qty": "1", "type": "limit",
                                                    "limit_price": "1.00", "tif": "gtc"})
    assert r.status_code == 200, r.text
    before = ex.accounts[aid]
    for bad in ("1e999999999", "1e-999999999", "2000000000", "-5", "abc", [1]):
        assert c.post(f"/api/accounts/{aid}/reset", json={"starting_cash": bad}).status_code == 400, bad
        assert ex.accounts[aid] is before
        assert [o.status for o in ex.orders(aid, "open")] != []
    ex.save(force=True)
    c2, ex2 = client(str(tmp_path))  # a restart loads the same state, and the list still answers
    assert c2.get("/api/accounts").status_code == 200 and aid in ex2.accounts


# ---- request body and entity-count limits ----------------------------------------------------------

def test_large_bodies_are_refused_with_413():
    c, ex = client()
    big = b'{"name": "' + b"x" * (auth.MAX_REQUEST_BODY + 10) + b'"}'
    r = c.post("/api/accounts", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and "too large" in r.json()["detail"]
    assert r.headers["cross-origin-resource-policy"] == "same-origin"

    def chunks():  # no Content-Length: the middleware counts the bytes itself
        for _ in range(auth.MAX_REQUEST_BODY // 65536 + 2):
            yield b" " * 65536
    r = c.post("/api/accounts", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert len(ex.accounts) == 1
    assert c.post("/api/accounts", json={"name": "ok", "type": "cash", "starting_cash": 10}).status_code == 200


def test_entity_counts_are_limited(monkeypatch):
    monkeypatch.setattr(exchange_mod, "MAX_ACCOUNTS", 2)
    monkeypatch.setattr(exchange_mod, "MAX_OPEN_ORDERS", 2)
    import app.api.server as server_mod
    monkeypatch.setattr(server_mod, "MAX_WATCHLISTS", 2)
    c, ex = client()
    assert c.post("/api/accounts", json={"name": "b"}).status_code == 200
    r = c.post("/api/accounts", json={"name": "c"})
    assert r.status_code == 400 and "already 2 accounts" in r.json()["detail"]
    aid = next(iter(ex.accounts))
    order = {"symbol": "NVLX", "side": "buy", "qty": "1", "type": "limit", "limit_price": "1.00", "tif": "gtc"}
    for _ in range(2):
        assert c.post(f"/api/accounts/{aid}/orders", json=order).status_code == 200
    r = c.post(f"/api/accounts/{aid}/orders", json=order)
    assert r.status_code == 422 and "2 working orders" in r.json()["detail"]
    names = [n for n in ("A", "B") if n not in ex.watchlists]
    for n in names[: 2 - len(ex.watchlists)]:
        assert c.put(f"/api/watchlists/{n}", json=[]).status_code == 200
    r = c.put("/api/watchlists/Another", json=[])
    assert r.status_code == 400 and "watchlists" in r.json()["detail"]
    existing = next(iter(ex.watchlists))
    assert c.put(f"/api/watchlists/{existing}", json=["NVLX"]).status_code == 200  # replacing one still works


# ---- symbol length cap before the option-symbol cache ------------------------------------------------

def test_long_symbols_never_reach_the_parser_cache():
    symbology._parse.cache_clear()
    for i in range(50):
        assert symbology.parse("NVLX261218C00150000" + "9" * (i + 3)) is None
    assert symbology.parse("x" * 100_000) is None and symbology.parse(None) is None
    assert symbology._parse.cache_info().currsize == 0
    c = symbology.parse("ABCDEF261218C00150000")  # the longest real OCC symbol still parses
    assert c is not None and c.strike == Decimal("150")


def test_websocket_symbols_are_truncated():
    c, _ = client()
    clients = c.app.state.ctx.clients
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"symbols": ["N" * 10_000, "nvlx"]})
        deadline = time.monotonic() + 5
        while not any(s["symbols"] for s in clients.values()) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [s["symbols"] for s in clients.values()] == [["N" * 32, "NVLX"]]


# ---- news links: http and https only ---------------------------------------------------------------

@pytest.mark.parametrize("url, kept", [("https://example.com/a?b=1", True), ("http://example.com", True),
                                       ("HTTPS://EXAMPLE.COM/x", True), ("javascript:alert(1)", False),
                                       (" JavaScript:alert(1)", False), ("data:text/html,<b>", False),
                                       ("//example.com/x", False), ("/relative", False), ("https:///nohost", False),
                                       ("https://e.com/\nx", False), ("https://e.com/" + "a" * 3000, False),
                                       (None, False), (5, False), (["https://e.com"], False)])
def test_web_url(url, kept):
    assert http.web_url(url) == (url.strip() if kept else "")


def test_provider_news_drops_unsafe_links():
    alp = research.news_records([{"id": 1, "headline": "h", "url": "javascript:alert(1)",
                                  "images": [{"size": "thumb", "url": "data:image/png;base64,AA"}, "junk"]},
                                 {"id": 2, "headline": "ok", "url": "https://news.example/a",
                                  "images": [{"size": "thumb", "url": "https://img.example/t.png"}]}])
    by_id = {r["id"]: r for r in alp}
    assert by_id[1]["url"] == "" and by_id[1]["image"] is None
    assert by_id[2]["url"] == "https://news.example/a" and by_id[2]["image"] == "https://img.example/t.png"
    fh = finnhub.news_records([{"id": 3, "url": "vbscript:x", "image": "javascript:y", "datetime": 1}])
    assert fh[0]["url"] == "" and fh[0]["image"] is None
    f = finnhub.fundamentals_record({"weburl": "javascript:z", "logo": "https://l.example/x.png"}, {})
    assert f["website"] is None and f["logo"] == "https://l.example/x.png"


# ---- marketdata/http.py: no redirects, bounded responses ----------------------------------------------

def test_transport_never_follows_redirects():
    t = http.UrllibTransport()
    handlers = t._opener.handlers
    assert any(isinstance(h, http._NoRedirect) for h in handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in handlers)
    req = urllib.request.Request("https://api.example/x", headers={"Authorization": "secret"})
    assert http._NoRedirect().redirect_request(req, None, 302, "Found", {}, "https://elsewhere.example/") is None


def test_responses_are_read_with_a_bound():
    assert http.read_bounded(io.BytesIO(b"x" * 10), 10, "h") == b"x" * 10
    with pytest.raises(http.ProviderError, match="larger than"):
        http.read_bounded(io.BytesIO(b"x" * 11), 10, "api.example")


def test_a_redirect_status_is_reported_not_followed():
    class Redirecting(http.Transport):
        def get(self, url, headers, timeout):
            return 302, {"Location": "https://elsewhere.example/"}, ""
    c = http.JsonClient("Test", {}, transport=Redirecting(), sleep=lambda s: None)
    with pytest.raises(http.ProviderError) as e:
        c.get("https://api.example", "/x")
    assert e.value.status == 302


# ---- research cache bounds --------------------------------------------------------------------------

def test_research_cache_is_bounded():
    cache = research._Cache(lambda: 0.0)
    n = cache.MAX_ROWS + 100
    for i in range(n):
        cache.get(("ca", "NVLX", i), 3600, lambda i=i: i)
    assert len(cache.rows) == cache.MAX_ROWS
    assert ("ca", "NVLX", 0) not in cache.rows and ("ca", "NVLX", n - 1) in cache.rows
    assert cache.get(("ca", "NVLX", n - 1), 3600, lambda: pytest.fail("cached"))[0] == n - 1


# ---- bounded, type-checked replay session reads ----------------------------------------------------------

def _lines():
    with gzip.open(SAMPLE, "rt", encoding="utf-8") as f:
        return f.read().splitlines()


def _write(path, lines):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _with_events(lines, events):
    """The sample's header and context lines, the given events, and a matching end marker."""
    ctx = [l for l in lines[1:] if l.startswith("{") and '"end"' not in l]
    return [lines[0], *ctx, *events, json.dumps({"end": {"events": len(events), "end": 1764352800.0}})]


def test_sample_sessions_still_load():
    data = fmt.read_session(SAMPLE)
    assert data.events and data.daily


@pytest.mark.parametrize("event", ['["Q","x","NVLX",1,2,3,4]', '["Q",1764340300,"NVLX",[1],2,3,4]',
                                   '["T",1764340300,"NVLX","1e999999999",1]', '["T",1764340300,"NVLX",1e999999,1]',
                                   '["T",1764340300,"NVLX",NaN,1]', '["T",1764340300,["NVLX"],1,1]',
                                   '["T",NaN,"NVLX",1,1]', '["T"]', '5', '"text"', '["T",1764340300,"NVLX",1,true]'])
def test_badly_typed_events_are_format_errors(tmp_path, event):
    path = _write(tmp_path / "s.pdsession.gz", _with_events(_lines(), [event]))
    with pytest.raises(fmt.SessionFormatError):
        fmt.read_session(path)


@pytest.mark.parametrize("ctx", ['{"daily":"NVLX","bars":"x"}', '{"daily":"NVLX","bars":[[1,2]]}',
                                 '{"minutes":"NVLX","bars":[["a",1,1,1,1,1]]}', '{"daily":["NVLX"],"bars":[]}'])
def test_badly_typed_bars_are_format_errors(tmp_path, ctx):
    lines = _lines()
    path = _write(tmp_path / "s.pdsession.gz", [lines[0], ctx, json.dumps({"end": {"events": 0, "end": 1}})])
    with pytest.raises(fmt.SessionFormatError):
        fmt.read_session(path)


@pytest.mark.parametrize("change", [{"symbols": "NVLX"}, {"symbols": ["NVLX"]}, {"initial": []}, {"start": "x"},
                                    {"start": None}, {"initial": {"MKTX": {"last": "1e999999"}}}])
def test_badly_typed_headers_are_format_errors(tmp_path, change):
    lines = _lines()
    h = json.loads(lines[0])
    h.update(change)
    path = _write(tmp_path / "s.pdsession.gz", [json.dumps(h), *lines[1:]])
    with pytest.raises(fmt.SessionFormatError):
        fmt.read_header(path)


def test_oversized_session_files_are_refused(tmp_path, monkeypatch):
    lines = _lines()
    monkeypatch.setattr(fmt, "MAX_LINE", 1000)
    path = _write(tmp_path / "long.pdsession.gz", [lines[0][:200] + " " * 2000, *lines[1:]])
    with pytest.raises(fmt.SessionFormatError, match="longer than"):
        fmt.read_header(path)
    monkeypatch.setattr(fmt, "MAX_LINE", 8 * 1024 * 1024)
    monkeypatch.setattr(fmt, "MAX_EVENTS", 100)
    with pytest.raises(fmt.SessionFormatError, match="more than 100 events"):
        fmt.read_session(SAMPLE)
    monkeypatch.setattr(fmt, "MAX_EVENTS", 5_000_000)
    monkeypatch.setattr(fmt, "MAX_TOTAL", 100_000)
    with pytest.raises(fmt.SessionFormatError, match="unpacked"):
        fmt.read_session(SAMPLE)


def test_a_damaged_session_is_refused_on_replay_start(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERDESK_DATA", str(tmp_path))
    c, ex = client(str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    _write(sessions / "bad-1.pdsession.gz", _with_events(_lines(), ['["Q",1764340300,"NVLX",{},2,3,4]']))
    listed = [s["id"] for s in c.get("/api/replay/sessions").json()]
    if "bad-1" not in listed:
        pytest.skip("this build does not read sessions from the data directory")
    r = c.post("/api/replay/start", json={"session": "bad-1"})
    assert r.status_code == 422 and "damaged" in r.json()["detail"], r.text


# ---- Cross-Origin-Resource-Policy and no cross-site /api GETs -----------------------------------------------

@pytest.mark.parametrize("env", [{}, {"PAPERDESK_PASSCODE": PASS}, {"PAPERDESK_LAN": "1", "PAPERDESK_PASSCODE": PASS}])
def test_cross_site_api_reads_are_refused(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    c, _ = client(client=("127.0.0.1", 5000))  # loopback bypass keeps the passcode out of the way
    for headers in ({"sec-fetch-site": "cross-site"}, {"sec-fetch-site": "same-site"},
                    {"origin": "http://evil.example"}, {"origin": "null"}):
        for path in ("/api/accounts", "/api/status", "/api/auth/state"):
            r = c.get(path, headers=headers)
            assert r.status_code == 403 and "another website" in r.json()["detail"], (path, headers)
            assert r.headers["cross-origin-resource-policy"] == "same-origin"
    for headers in ({}, {"sec-fetch-site": "same-origin"}, {"sec-fetch-site": "none"},
                    {"sec-fetch-site": "same-origin", "origin": "http://testserver"}):
        r = c.get("/api/accounts", headers=headers)
        assert r.status_code == 200, headers
        assert r.headers["cross-origin-resource-policy"] == "same-origin"


def test_lan_passcode_sign_in_still_works_from_the_same_origin(monkeypatch):
    monkeypatch.setenv("PAPERDESK_LAN", "1")
    monkeypatch.setenv("PAPERDESK_PASSCODE", PASS)
    monkeypatch.setenv("PAPERDESK_LOOPBACK_BYPASS", "0")
    c, _ = client(client=("192.168.1.20", 5000), base_url="http://192.168.1.5:8000")
    same = {"origin": "http://192.168.1.5:8000", "sec-fetch-site": "same-origin"}
    assert c.get("/api/accounts", headers={"sec-fetch-site": "same-origin"}).status_code == 401
    assert c.post("/api/auth/login", json={"passcode": PASS}, headers=same).json() == {"ok": True}
    assert c.get("/api/accounts", headers={"sec-fetch-site": "same-origin"}).status_code == 200
    assert c.get("/api/accounts", headers={"sec-fetch-site": "cross-site"}).status_code == 403


# ---- /docs off by default ---------------------------------------------------------------------------------------

def test_api_docs_are_off_by_default(monkeypatch):
    monkeypatch.delenv("PAPERDESK_API_DOCS", raising=False)
    c, _ = client()
    paths = {getattr(r, "path", None) for r in c.app.routes}
    assert not paths & {"/docs", "/redoc", "/openapi.json"}
    r = c.get("/openapi.json")
    assert r.status_code == 404 or "openapi" not in r.text


def test_api_docs_can_be_turned_on(monkeypatch):
    monkeypatch.setenv("PAPERDESK_API_DOCS", "1")
    c, _ = client()
    assert c.get("/openapi.json").json()["info"]["title"] == "PaperDesk"
    assert c.get("/docs").status_code == 200
