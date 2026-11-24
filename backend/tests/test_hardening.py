"""Regression tests for the security review's hardening findings (numbers, limits, streams, files, providers)."""
import gzip
import io
import json
import os
import time
from decimal import Decimal

import anyio
import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

from app.api import hardening
from app.api.server import create_app, docs_settings
from app.core import limits
from app.core.money import MAX_MAGNITUDE, check_price, parse, parse_cash, parse_percent
from app.exchange import Exchange, OrderRejected
from app.ledger.account import Account
from app.marketdata import http as mhttp
from app.marketdata.research import ResearchHub, _Cache, clamp_range, clean_types, news_records
from app.marketdata.finnhub import news_records as finnhub_news
from app.marketdata.replay import format as fmt
from app.marketdata.replay.library import list_sessions
from app.marketdata.synthetic import SyntheticFeed
from app.options import symbology

D = Decimal


def client(**kw):
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()))
    ex.create_account("Margin", "margin")
    ex.tick()
    return TestClient(create_app(ex, run_loop=False, **kw), raise_server_exceptions=False), ex


# ---- 1. numbers ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,msg", [
    ("1e-150000000", "too small"), ("1E-999999", "too small"), ("1e999999", "too large"), ("1e13", "too large"),
    (10 ** 40, "too large"), ("abc", "must be a number"), ({"a": 1}, "must be a number"), ([1], "must be a number"),
    (True, "must be a number"), (None, "missing"), ("", "missing"), ("1" * 41, "too long"), ("NaN", "finite"),
    ("Infinity", "finite"), ("1.123456789", "8 decimal places"), (float("inf"), "finite"),
])
def test_parse_refuses_extreme_and_non_numeric_input(raw, msg):
    with pytest.raises(ValueError, match=msg):
        parse(raw, "limit price")


def test_parse_keeps_ordinary_numbers_exact():
    assert parse("123.45") == D("123.45") and parse(100000) == D(100000) and parse(0.1) == D("0.1")
    assert parse("1.5E+3") == D(1500) and str(parse("1e3")) == "1000"
    assert parse(0.1 + 0.2) == D("0.30000000")  # a float artefact is rounded, not refused
    assert parse("100.000000000000") == D(100) and parse("-0") == 0 and str(parse("-0")) == "0"


@settings(max_examples=400, deadline=None)
@given(st.one_of(st.text(max_size=60), st.floats(allow_nan=True, allow_infinity=True), st.integers(),
                 st.decimals(allow_nan=True, allow_infinity=True), st.none(), st.booleans(),
                 st.lists(st.integers(), max_size=2), st.dictionaries(st.text(max_size=3), st.integers(), max_size=2)))
def test_parse_property_only_valueerror_and_bounded(x):
    try:
        d = parse(x)
    except ValueError:
        return
    assert d.is_finite() and abs(d) <= MAX_MAGNITUDE
    assert d.as_tuple().exponent >= -8
    assert len(f"{d:f}") < 40  # cheap to format, store and export


def test_field_domains():
    assert check_price(D("1.25")) == D("1.25") and check_price(D("0.1234")) == D("0.1234")
    for bad in ("123.456789", "0", "-1", "0.00001", "2000000000"):
        with pytest.raises(ValueError):
            check_price(parse(bad))
    assert parse_cash("25000") == D(25000)
    for bad in ("0", "-5", "1000000000.01", "10.001", "1e-150000000", "1e999999"):
        with pytest.raises(ValueError):
            parse_cash(bad)
    assert parse_percent("2.5") == D("2.5")
    for bad in ("0", "100", "1.00001"):
        with pytest.raises(ValueError):
            parse_percent(bad)


def test_api_refuses_extreme_numbers_with_4xx_and_names_the_field():
    c, ex = client()
    aid = next(iter(ex.accounts))
    for cash in ("1e-150000000", "1e999999", "abc", {"a": 1}, [1, 2], "0"):
        r = c.post("/api/accounts", json={"starting_cash": cash})
        assert r.status_code == 400 and "starting cash" in r.json()["detail"].lower(), (cash, r.text)
    r = c.post("/api/alerts", json={"symbol": "NVLX", "op": "above", "price": "1E-999999"})
    assert r.status_code == 400 and "alert price" in r.json()["detail"].lower()
    assert c.post("/api/alerts", json={"symbol": "NVLX", "op": "above", "price": "12.345"}).status_code == 400
    for k in ("trail_amount", "trail_percent"):
        body = {"symbol": "NVLX", "side": "sell", "qty": 1, "type": "trailing_stop", "tif": "gtc", k: "1e-999999"}
        r = c.post(f"/api/accounts/{aid}/orders", json=body)
        assert r.status_code == 422 and "trail" in r.json()["detail"].lower(), r.text
    body = {"symbol": "NVLX", "side": "sell", "qty": 1, "type": "trailing_stop", "tif": "gtc", "trail_amount": "1.234"}
    assert c.post(f"/api/accounts/{aid}/orders", json=body).status_code == 422  # off the tick grid


def test_replace_applies_the_new_order_price_domain():
    c, ex = client()
    aid = next(iter(ex.accounts))
    px = ex.quote("NVLX").last
    limit = str((px * D("0.5")).quantize(D("0.01")))
    oid = c.post(f"/api/accounts/{aid}/orders", json={"symbol": "NVLX", "side": "buy", "qty": 1, "type": "limit",
                                                      "limit_price": limit, "tif": "gtc"}).json()[0]["id"]
    for bad in ("123.456789", "1e-999999", "0.00001", "abc", {"x": 1}):
        r = c.patch(f"/api/accounts/{aid}/orders/{oid}", json={"limit_price": bad})
        assert r.status_code == 422, (bad, r.status_code, r.text)
        assert ex.engine.orders[oid].limit_price == D(limit)
    assert c.patch(f"/api/accounts/{aid}/orders/{oid}", json=["not", "an", "object"]).status_code == 422


def test_reset_validates_before_replacing_and_create_reset_share_limits():
    c, ex = client()
    aid = next(iter(ex.accounts))
    before = ex.accounts[aid]
    for cash in ("1e999999", "1000000000.01", "-1", "abc", "1.001"):
        r = c.post(f"/api/accounts/{aid}/reset", json={"starting_cash": cash})
        assert r.status_code == 400, (cash, r.text)
        assert ex.accounts[aid] is before  # nothing was replaced
    assert c.get("/api/accounts").status_code == 200
    assert c.post(f"/api/accounts/{aid}/reset", json={"starting_cash": "1000000000"}).status_code == 200
    assert c.post("/api/accounts", json={"starting_cash": "1000000000"}).status_code == 200
    with pytest.raises(ValueError):
        ex.reset_account(aid, D("1e999999"))  # the exchange checks too, whoever calls it


def test_csv_export_of_a_small_account_is_small_and_fast():
    c, ex = client()
    aid = c.post("/api/accounts", json={"starting_cash": "0.01"}).json()["id"]
    t = time.monotonic()
    r = c.get(f"/api/accounts/{aid}/export/ledger.csv")
    assert r.status_code == 200 and len(r.content) < 2000 and time.monotonic() - t < 2


# ---- 1. self-healing: one account that cannot be valued ---------------------------------------------------------

def _break(ex) -> str:
    """Plant an account the way an old state file could: a starting cash no request can create any more."""
    a = Account.from_plain(ex.create_account("Broken", "margin").to_plain())
    a.starting_cash = D("1e999999")
    a.ledger.post(0, "deposit", "bad", [("cash", D("1e999999")), ("equity", D("-1e999999"))])
    ex.accounts[a.id] = a
    return a.id


def test_one_broken_account_does_not_take_down_the_list_tick_or_stream():
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
    # reset without new cash keeps the impossible number, so it is refused; with new cash it is repaired
    assert c.post(f"/api/accounts/{bad}/reset", json={}).status_code == 400
    assert c.post(f"/api/accounts/{bad}/reset", json={"starting_cash": "5000"}).status_code == 200
    assert "broken" not in {r["id"]: r for r in c.get("/api/accounts").json()}[bad]
    bad2 = _break(ex)
    assert c.delete(f"/api/accounts/{bad2}").status_code == 200
    assert bad2 not in ex.accounts and bad2 not in ex.faults


# ---- 2. WebSocket --------------------------------------------------------------------------------------------------

def test_bad_stream_message_closes_only_that_socket_and_others_keep_ticking():
    c, ex = client()
    aid = next(iter(ex.accounts))
    bad = _break(ex)
    app = c.app
    with c.websocket_connect("/ws") as good_ws, c.websocket_connect("/ws") as broken_acct:
        good_ws.send_text(json.dumps({"symbols": ["NVLX"], "account": aid}))
        broken_acct.send_text(json.dumps({"account": bad}))
        with c.websocket_connect("/ws") as evil:
            evil.send_text(json.dumps({"account": [1, 2]}))
            msg = evil.receive()
            assert msg["type"] == "websocket.close" and msg["code"] == limits.WS_CLOSE_POLICY
        for payload, code in (("x" * (limits.MAX_WS_MESSAGE + 1), limits.WS_CLOSE_TOO_BIG), ("not json", 1008),
                              (json.dumps({"symbols": ["A" * 100]}), 1008), (json.dumps({"symbols": {"a": 1}}), 1008)):
            with c.websocket_connect("/ws") as evil:
                evil.send_text(payload)
                assert evil.receive()["code"] == code
        deadline = time.monotonic() + 5
        while len(app.state.ctx.clients) != 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        ex.events.append({"type": "order", "account_id": aid, "order": {"id": "X"}})
        anyio.run(app.state.broadcast, ex)
        m = good_ws.receive_json()
        assert m["type"] == "tick" and "NVLX" in m["quotes"] and m["account"]["id"] == aid
        assert [e["order"]["id"] for e in m["events"]] == ["X"]  # events are not lost
        m2 = broken_acct.receive_json()
        assert m2["account"]["broken"] is True


def test_stream_connection_limits(monkeypatch):
    monkeypatch.setattr(limits, "MAX_WS_PER_ADDRESS", 2)
    c, ex = client()
    with c.websocket_connect("/ws"), c.websocket_connect("/ws"):
        with pytest.raises(Exception):
            with c.websocket_connect("/ws") as third:
                third.receive()


def test_stream_symbol_count_is_capped():
    sub = {"symbols": [], "account": None}
    hardening.ws_update(json.dumps({"symbols": ["S%d" % i for i in range(1000)]}), sub)
    assert len(sub["symbols"]) == limits.MAX_WS_SYMBOLS
    for bad in ('{"account": 5}', '{"account": {"a": 1}}', "[1]", None):
        with pytest.raises(hardening.BadMessage):
            hardening.ws_update(bad, dict(sub))


# ---- 3. sizes and counts ---------------------------------------------------------------------------------------------

def test_request_body_limit_by_length_and_by_counting():
    c, ex = client()
    big = json.dumps({"name": "x" * (limits.MAX_BODY_BYTES + 10)})
    r = c.post("/api/accounts", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and "64 KB" in r.json()["detail"]

    def chunks():  # no Content-Length: the bytes are counted as they arrive
        for _ in range(100):
            yield b" " * 1024
    r = c.post("/api/accounts", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert len(ex.accounts) == 1
    # a route can declare a larger limit; chart drawings do
    rec = {"updated": 1, "data": [{"t": "x" * 200_000}]}
    assert c.put("/api/chartstate/drawings/NVLX", json=rec).status_code == 200


def test_allow_body_for_one_route():
    c, ex = client()
    app = c.app

    @app.post("/api/test-upload/{n}")
    async def upload(n: str, request: __import__("fastapi").Request):
        return {"n": len(await request.body())}
    hardening.allow_body(app, "/api/test-upload/{n}", 200 * 1024)
    assert c.post("/api/test-upload/1", content=b"x" * 150_000).json() == {"n": 150_000}
    assert c.post("/api/test-upload/1", content=b"x" * 250_000).status_code == 413
    assert hardening.body_limit_for(app, "/api/accounts") == limits.MAX_BODY_BYTES


@pytest.mark.parametrize("limit,make", [
    ("MAX_ACCOUNTS", lambda c, aid, i: c.post("/api/accounts", json={"name": f"a{i}"})),
    ("MAX_ALERTS", lambda c, aid, i: c.post("/api/alerts", json={"symbol": "NVLX", "op": "above", "price": "1"})),
    ("MAX_WATCHLISTS", lambda c, aid, i: c.put(f"/api/watchlists/w{i}", json=["NVLX"])),
])
def test_count_caps(monkeypatch, limit, make):
    monkeypatch.setattr(limits, limit, 3)
    c, ex = client()
    aid = next(iter(ex.accounts))
    codes = [make(c, aid, i).status_code for i in range(6)]
    assert 400 in codes and codes[-1] == 400 and codes.count(200) <= 3
    assert "at most 3" in make(c, aid, 99).json()["detail"]


def test_watchlist_symbol_cap_and_journal_caps(monkeypatch):
    c, ex = client()
    aid = next(iter(ex.accounts))
    assert c.put("/api/watchlists/Big", json=["NVLX"] * (limits.MAX_WATCHLIST_SYMBOLS + 1)).status_code == 400
    assert c.put("/api/watchlists/Odd", json=[{"a": 1}, ["x"], "nvlx", "Z" * 5000]).json()["Odd"] == ["NVLX"]
    oid = c.post(f"/api/accounts/{aid}/orders", json={"symbol": "NVLX", "side": "buy", "qty": 1}).json()[0]["id"]
    base = f"/api/accounts/{aid}/journal"
    assert c.post(base, json={"target_type": "order", "target_id": oid, "note": "x" * 4001}).status_code == 400
    assert c.post(base, json={"target_type": "order", "target_id": oid, "note": {"a": 1}}).status_code == 400
    monkeypatch.setattr("app.analytics.journal.MAX_ENTRIES", 2)
    codes = [c.post(base, json={"target_type": "order", "target_id": oid, "note": "n"}).status_code for _ in range(3)]
    assert codes == [200, 200, 400]


def test_working_order_cap(monkeypatch):
    monkeypatch.setattr(limits, "MAX_WORKING_ORDERS", 2)
    c, ex = client()
    aid = next(iter(ex.accounts))
    px = ex.quote("NVLX").last
    body = {"symbol": "NVLX", "side": "buy", "qty": 1, "type": "limit", "tif": "gtc",
            "limit_price": str((px * D("0.5")).quantize(D("0.01")))}
    codes = [c.post(f"/api/accounts/{aid}/orders", json=body).status_code for _ in range(3)]
    assert codes == [200, 200, 422]


def test_long_symbols_never_reach_the_cache_and_are_not_echoed():
    c, ex = client()
    aid = next(iter(ex.accounts))
    before = symbology._parse.cache_info().currsize
    for i in range(20):
        sym = f"{i}" + "X" * 60_000
        r = c.post(f"/api/accounts/{aid}/orders/preview", json={"symbol": sym, "side": "buy", "qty": 1})
        assert r.status_code == 422 and len(r.content) < 1000
    assert symbology._parse.cache_info().currsize == before
    assert symbology.parse("NVLX261218C00150000").strike == 150
    r = c.post("/api/alerts", json={"symbol": "Q" * 60_000, "op": "above", "price": 1})
    assert r.status_code == 400 and len(r.content) < 1000


def test_validation_errors_do_not_echo_the_input():
    c, ex = client()
    r = c.post("/api/accounts", json=["x" * 50_000])
    assert r.status_code == 422 and len(r.content) < 2000 and "xxxx" not in r.text
    r = c.get("/api/bars/NVLX", params={"limit": "y" * 5000})
    assert r.status_code == 422 and len(r.content) < 2000


# ---- 5. news links ------------------------------------------------------------------------------------------------------

def test_news_links_are_http_or_https_only():
    rows = [{"id": i, "headline": "h", "url": u, "images": [{"size": "thumb", "url": u}]}
            for i, u in enumerate(["javascript:alert(1)", " JAVASCRIPT:alert(1)", "data:text/html,x", "/relative",
                                   "https://ok.example/a", "http://ok.example/b", None, 5, "https://x/\nfoo"])]
    out = {r["id"]: r for r in news_records(rows)}
    assert [out[i]["url"] for i in range(9)] == ["", "", "", "", "https://ok.example/a", "http://ok.example/b", "", "", ""]
    assert out[0]["image"] is None and out[4]["image"] == "https://ok.example/a"
    f = finnhub_news([{"id": 1, "url": "javascript:x", "image": "vbscript:y", "datetime": 1}])
    assert f[0]["url"] == "" and f[0]["image"] is None


# ---- 6. provider HTTP -------------------------------------------------------------------------------------------------------

class _Resp:
    def __init__(self, body: bytes, length=None):
        self.buf, self.headers = io.BytesIO(body), {"Content-Length": str(length)} if length is not None else {}

    def read(self, n=-1):
        return self.buf.read(n)


def test_provider_responses_are_capped_and_redirects_refused():
    assert mhttp.read_capped(_Resp(b"x" * 1000), 2000, time.monotonic() + 5) == b"x" * 1000
    with pytest.raises(mhttp.ResponseTooLarge):
        mhttp.read_capped(_Resp(b"x" * 3000), 2000, time.monotonic() + 5)
    with pytest.raises(mhttp.ResponseTooLarge):
        mhttp.read_capped(_Resp(b"", length=10 ** 9), 2000, time.monotonic() + 5)
    with pytest.raises(OSError):
        mhttp.read_capped(_Resp(b"x" * 200_000), 10 ** 6, time.monotonic() - 1)
    assert mhttp._RefuseRedirects().redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None
    t = mhttp.UrllibTransport()
    assert any(isinstance(h, mhttp._RefuseRedirects) for h in t.opener.handlers)

    class Redirecting(mhttp.Transport):
        calls = 0

        def get(self, url, headers, timeout):
            Redirecting.calls += 1
            return 302, {"Location": "https://evil.example/"}, ""
    c = mhttp.JsonClient("Alpaca", {"APCA-API-KEY-ID": "k"}, Redirecting(), mhttp.RateLimiter(100), sleep=lambda s: None)
    with pytest.raises(mhttp.ProviderError, match="redirect"):
        c.get("https://data.alpaca.markets", "/x")
    assert Redirecting.calls == 1
    with pytest.raises(mhttp.ProviderError, match="https"):
        c.get("http://data.alpaca.markets", "/x")
    assert Redirecting.calls == 1  # refused before any request was made


def test_provider_numbers_are_tolerant_and_counted():
    n = mhttp.invalid_count()
    assert mhttp.dec("abc") is None and mhttp.dec({"a": 1}) is None and mhttp.dec("NaN") is None
    assert mhttp.dec("1e999999") is None and mhttp.dec(10 ** 40) is None
    assert mhttp.price("-1") is None and mhttp.price("2e7") is None
    assert mhttp.invalid_count() == n + 7 and "invalid" in mhttp.invalid_status()
    assert mhttp.price(0) is None and mhttp.invalid_count() == n + 7  # a zero side is "no quote", not bad data
    assert mhttp.dec("1.25") == D("1.25") and mhttp.price(101.5) == D("101.5")
    from app.marketdata.alpaca import option_quote_from_snapshot
    state = __import__("app.marketdata.alpaca", fromlist=["SymbolState"])
    assert option_quote_from_snapshot("X", {"latestQuote": {"bp": "junk", "ap": "NaN"}, "latestTrade": {"p": -3}}) is None
    sym_state = [v for v in vars(state).values() if isinstance(v, type) and hasattr(v, "apply_quote")][0]("ABC")
    sym_state.apply_quote({"bp": "abc", "ap": {"x": 1}, "bs": "?", "as": None}, 1.0)
    assert sym_state.to_quote() is None
    assert sym_state.apply_trade({"p": "1e999999", "s": 5}, 2.0, True) is False


# ---- 7. research -------------------------------------------------------------------------------------------------------------

def test_research_types_dates_and_cache_are_bounded():
    assert clean_types("forward_split, cash_dividend,cash_dividend") == "cash_dividend,forward_split"
    assert clean_types(None) is None and clean_types("") is None
    with pytest.raises(ValueError, match="unknown corporate action type"):
        clean_types("x" * 1000)
    from datetime import date
    today = date(2026, 11, 24)
    s, e = clamp_range(date(1900, 1, 1), date(2999, 1, 1), today)
    assert (e - s).days <= limits.RESEARCH_MAX_RANGE_DAYS and s >= date(2021, 1, 1) and e <= date(2027, 11, 24)
    with pytest.raises(ValueError):
        clamp_range(date(2026, 5, 1), date(2026, 1, 1), today)
    now = [0.0]
    cache = _Cache(lambda: now[0], max_rows=3, max_age=100)
    for i in range(10):
        cache.get(i, 50, lambda i=i: i)
    assert list(cache.rows) == [7, 8, 9]
    cache.get(7, 50, lambda: "unused")  # a hit becomes most recent
    assert list(cache.rows) == [8, 9, 7]
    now[0] = 1000
    cache.get("new", 50, lambda: 1)
    assert list(cache.rows) == ["new"]  # old entries expire


class _FakeAlpaca:
    def __init__(self):
        self.calls = 0

    def corporate_actions(self, symbol, start, end, types, max_wait=5):
        self.calls += 1
        return {}

    def news(self, symbol, limit, max_wait=5):
        self.calls += 1
        return []


def test_research_routes_validate_and_rate_limit(monkeypatch):
    c, ex = client()
    fake = _FakeAlpaca()
    ex.feed.research = ResearchHub(alpaca=fake)
    assert c.get("/api/research/corporate-actions/NVLX", params={"types": "bogus"}).status_code == 400
    assert c.get("/api/research/corporate-actions/NVLX", params={"start": "2026-13-01"}).status_code == 400
    for t in ("cash_dividend", "cash_dividend,cash_dividend", " cash_dividend"):
        assert c.get("/api/research/corporate-actions/NVLX", params={"types": t}).status_code == 200
    assert fake.calls == 1  # equal requests share one cache entry
    codes = [c.get("/api/research/news/NVLX").status_code for _ in range(limits.RESEARCH_RATE_BURST + 5)]
    assert 429 in codes
    r = c.get("/api/research/news/NVLX")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


# ---- 8. replay session files ---------------------------------------------------------------------------------------------------

def _session(path, header=None, lines=(), raw_first=None):
    h = {"format": "paperdesk-session", "version": 1, "date": "2026-01-02", "start": 100,
         "symbols": [{"symbol": "ABC"}], "initial": {"ABC": {"bid": "1", "ask": "1.01", "last": "1"}}}
    h.update(header or {})
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(raw_first if raw_first is not None else json.dumps(h))
        f.write("\n")
        for line in lines:
            f.write(line + "\n")


def test_bad_session_files_are_listed_with_an_error_not_a_500(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    _session(d / "a-good.pdsession.gz", lines=['["Q",101,"ABC",1,1.01,100,100]', '{"end":{"events":1,"end":102}}'])
    _session(d / "b-ints.pdsession.gz", header={"symbols": [1, 2]})
    _session(d / "c-initial.pdsession.gz", header={"initial": {"ABC": {"bid": "NaN"}}})
    _session(d / "d-types.pdsession.gz", header={"symbols": "ABC", "initial": []})
    _session(d / "e-long.pdsession.gz", raw_first="x" * (limits.MAX_SESSION_HEADER + 10))
    (d / "f-junk.pdsession.gz").write_bytes(b"not gzip at all")
    t = time.monotonic()
    rows = {r["id"]: r for r in list_sessions([str(d)])}
    assert time.monotonic() - t < 5
    assert "error" not in rows["a-good"]
    for sid in ("b-ints", "c-initial", "d-types", "e-long", "f-junk"):
        assert rows[sid]["error"], sid
    assert fmt.read_session(str(d / "a-good.pdsession.gz")).events[0][3] == D(1)


@pytest.mark.parametrize("event", ['["Q",101,"ABC",NaN,1.01,100,100]', '["Q",101,"ABC","NaN",1.01,100,100]',
                                   '["T",101,"ABC",-1,5]', '["T",101,"ABC",1e999,5]', '["T","x","ABC",1,5]',
                                   '["T",101,["ABC"],1,5]', '["T"]', '{"daily":"ABC","bars":[[1,"a",2,3,4,5]]}',
                                   '{"daily":["ABC"],"bars":[]}', '"just a string"'])
def test_session_events_must_be_finite_valid_numbers(tmp_path, event):
    p = tmp_path / "s.pdsession.gz"
    _session(p, lines=[event, '{"end":{"events":1,"end":102}}'])
    with pytest.raises(fmt.SessionFormatError):
        fmt.read_session(str(p))


def test_replay_listing_route_survives_a_crafted_file(tmp_path):
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()), data_dir=str(tmp_path))
    ex.create_account("Margin", "margin")
    (tmp_path / "sessions").mkdir()
    _session(tmp_path / "sessions" / "crafted.pdsession.gz", header={"symbols": [1, 2]})
    c = TestClient(create_app(ex, run_loop=False), raise_server_exceptions=False)
    r = c.get("/api/replay/sessions")
    assert r.status_code == 200 and any(x["id"] == "crafted" and x["error"] for x in r.json())


# ---- 9. access headers, docs, permissions ------------------------------------------------------------------------------------------

def test_cross_site_api_gets_are_refused_and_corp_is_set():
    c, ex = client()
    r = c.get("/api/accounts", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403 and r.headers["cross-origin-resource-policy"] == "same-origin"
    for site in ("same-origin", "none", None):
        r = c.get("/api/accounts", headers={"sec-fetch-site": site} if site else {})
        assert r.status_code == 200 and r.headers["cross-origin-resource-policy"] == "same-origin"


def test_api_docs_are_off_by_default():
    c, ex = client()
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(p).status_code in (404, 200) and "swagger" not in c.get(p).text.lower()
    assert c.get("/openapi.json").status_code != 200 or "openapi" not in c.get("/openapi.json").text
    assert docs_settings({}) == {"docs_url": None, "redoc_url": None, "openapi_url": None}
    assert docs_settings({"PAPERDESK_API_DOCS": "1"}) == {}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_permission_checks(tmp_path):
    d = tmp_path / "data"
    hardening.private_dir(str(d))
    assert (os.stat(d).st_mode & 0o777) == 0o700
    env = tmp_path / ".env"
    env.write_text("PAPERDESK_PASSCODE=x\n")
    os.chmod(env, 0o644)
    warnings = hardening.permission_warnings([str(d), str(env), str(tmp_path / "missing")])
    assert len(warnings) == 1 and "chmod 600" in warnings[0]
