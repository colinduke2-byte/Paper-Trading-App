"""Alpaca adapter tests. No network: every response is a saved documentation example
(see tests/fixtures/SOURCES.txt) served by a fake transport or a scripted websocket."""
import copy
import json
import threading
import time
import urllib.parse
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata import alpaca as al
from app.marketdata.alpaca import AlpacaFeed, StreamClient, StreamProtocol, SymbolState, build_chain, parse_bars
from app.marketdata.http import JsonClient, ProviderError, RateLimited, RateLimiter, Transport, loads, parse_ts
from app.options.symbology import parse as parse_option

FIX = Path(__file__).parent / "fixtures"
D = Decimal


def fixture(name: str):
    return loads((FIX / name).read_text(encoding="utf-8"))


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class FakeTransport(Transport):
    """Serves fixtures by URL path. A route value is a body, a (status, body[, headers]) tuple,
    or a list of those served in turn."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests = []

    def get(self, url, headers, timeout):
        parts = urllib.parse.urlsplit(url)
        self.requests.append({"url": url, "host": parts.hostname, "path": parts.path,
                              "params": dict(urllib.parse.parse_qsl(parts.query)), "headers": dict(headers)})
        r = self.routes.get(parts.path)
        if r is None:
            return 404, {}, json.dumps({"message": "not found"})
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if callable(r):
            r = r(self.requests[-1])
        if isinstance(r, tuple):
            status, body, *rest = r
            return status, (rest[0] if rest else {}), body if isinstance(body, str) else json.dumps(body, default=str)
        return 200, {}, r if isinstance(r, str) else json.dumps(r, default=str)


def raw(name):
    return (FIX / name).read_text(encoding="utf-8")


def routes(**extra):
    r = {"/v2/assets": raw("alpaca/assets.json"), "/v2/stocks/snapshots": raw("alpaca/stock_snapshots.json"),
         "/v2/stocks/bars": raw("alpaca/stock_bars.json"), "/v1beta1/news": raw("alpaca/news.json"),
         "/v1/corporate-actions": raw("alpaca/corporate_actions.json"),
         "/v1beta1/options/snapshots/AAPL": raw("alpaca/option_chain.json"),
         "/v1beta1/options/snapshots": raw("alpaca/option_chain.json")}
    r.update(extra)
    return r


class Wall:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_feed(t="2022-08-16T15:00:00Z", **kw):
    wall = Wall(ts(t))
    tr = FakeTransport(kw.pop("routes", None) or routes())
    feed = AlpacaFeed("PKTESTKEY", "test-secret", transport=tr, connector=kw.pop("connector", None),
                      sleep=lambda s: None, wall_clock=wall, **kw)
    return feed, tr, wall


# ---- parsing ------------------------------------------------------------------------------------
def test_parse_timestamps_with_nanoseconds():
    assert parse_ts("2021-02-22T15:51:45.335689322Z") == pytest.approx(ts("2021-02-22T15:51:45.335689Z"))
    assert parse_ts("2022-08-16T04:00:00Z") == ts("2022-08-16T04:00:00Z")
    assert parse_ts("2024-04-22T19:59:59-04:00") == ts("2024-04-22T23:59:59Z")
    assert parse_ts("") is None and parse_ts("soon") is None


def test_json_numbers_are_exact_decimals():
    j = fixture("alpaca/stock_latest_quotes.json")
    q = j["quotes"]["AAPL"]
    assert q["ap"] == D("172.7") and isinstance(q["ap"], Decimal) and q["as"] == 1


def test_snapshot_to_quote_during_the_session():
    snaps = fixture("alpaca/stock_snapshots.json")
    st = SymbolState("AAPL")
    assert st.apply_snapshot(snaps["AAPL"], date(2022, 8, 16))
    q = st.to_quote()
    assert (q.bid, q.ask, q.last) == (D("172.6"), D("172.7"), D("172.61"))
    assert (q.bid_size, q.ask_size) == (D(200), D(500))  # round lots -> shares
    assert (q.open, q.high, q.low) == (D("172.62"), D("173.71"), D("171.6618"))
    assert q.prev_close == D("173.19") and q.volume == D(56457696)
    assert q.trade is False  # a snapshot is not a new print
    assert all(isinstance(v, Decimal) for v in (q.bid, q.ask, q.last, q.volume, q.prev_close))
    assert q.ts == pytest.approx(ts("2022-08-17T10:18:27.052763Z"))


def test_snapshot_before_the_first_trade_of_the_day():
    snaps = fixture("alpaca/stock_snapshots.json")
    st = SymbolState("TSLA")
    st.apply_snapshot(snaps["TSLA"], date(2022, 8, 17))  # dailyBar is the previous session
    q = st.to_quote()
    assert q.prev_close == D("919.69") and q.open is None and q.volume == 0
    assert (q.bid, q.ask, q.last) == (D("911.31"), D("911.75"), D("911.99"))


def test_one_sided_and_crossed_quotes_never_price_at_zero():
    st = SymbolState("X")
    st.apply_trade({"p": D("10.00"), "s": 5}, 1.0, count_volume=True)
    st.apply_quote({"bp": D("0"), "bs": 0, "ap": D("10.05"), "as": 2}, 2.0)
    q = st.to_quote()
    assert q.bid == D("10.00") and q.ask == D("10.05")
    st.apply_quote({"bp": D("10.10"), "bs": 1, "ap": D("10.00"), "as": 1}, 3.0)
    q = st.to_quote()
    assert q.ask >= q.bid > 0
    st.apply_quote({"bp": D("0"), "bs": 0, "ap": D("0"), "as": 0}, 4.0)
    q = st.to_quote()
    assert q.bid == q.ask == D("10.00")
    assert SymbolState("NONE").to_quote() is None
    # an older message never overwrites a newer one
    assert not st.apply_quote({"bp": D("1"), "ap": D("2")}, 0.5)


def test_parse_bars_daily_and_intraday():
    rows = fixture("alpaca/stock_bars.json")["bars"]["AAPL"]
    intraday = parse_bars(rows, daily=False)
    assert intraday == [{"time": int(ts("2022-01-03T09:00:00Z")), "open": 178.26, "high": 178.26, "low": 178.21,
                         "close": 178.21, "volume": 1118.0}]
    daily = parse_bars([{"t": "2022-08-16T04:00:00Z", "o": 1, "h": 2, "l": D("0.5"), "c": D("1.5"), "v": 10},
                        {"t": "2022-08-15T04:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}], daily=True)
    assert [b["time"] for b in daily] == [int(ts("2022-08-15T00:00:00Z")), int(ts("2022-08-16T00:00:00Z"))]


def test_option_chain_from_provider_with_model_fallback_per_leg():
    snaps = fixture("alpaca/option_chain.json")["snapshots"]
    now = ts("2024-04-22T20:00:00Z")
    rows = build_chain("AAPL", date(2024, 4, 26), snaps, 166.0, 0.25, now, "indicative",
                       {"AAPL240426C00162500": "1234"})
    assert len(rows) == 1 and rows[0]["strike"] == D("162.5")
    call, put = rows[0]["call"], rows[0]["put"]
    assert call["source"] == "indicative" and call["greeks_source"] == "indicative"
    assert (call["bid"], call["ask"], call["last"]) == (D("4.15"), D("4.3"), D("4.1"))
    assert call["mid"] == D("4.225") and call["bid_size"] == D(16) and call["ask_size"] == D(91)
    assert call["delta"] == pytest.approx(0.7521304109871954) and call["iv"] == pytest.approx(0.3372405712050441)
    assert call["open_interest"] == 1234
    assert put["source"] == "model" and put["symbol"] == "AAPL240426P00162500" and put["ask"] > put["bid"]


def test_option_snapshot_without_greeks_gets_model_greeks():
    snap = {"latestQuote": {"ap": D("1.10"), "as": 3, "bp": D("1.00"), "bs": 2, "t": "2024-04-22T19:59:59Z"}}
    c = parse_option("AAPL240426C00170000")
    leg = al.option_leg(c, snap, 166.0, 0.25, ts("2024-04-22T20:00:00Z"), "indicative")
    assert leg["source"] == "indicative" and leg["greeks_source"] == "model"
    assert 0 < leg["delta"] < 1 and leg["iv"] > 0


# ---- stream protocol ------------------------------------------------------------------------------
S = fixture("alpaca/stream_session.json")


def ready_protocol(wanted=("AAPL", "AMD"), cap=30):
    p = StreamProtocol("PKTESTKEY", "test-secret", cap=cap)
    p.connecting()
    p.set_wanted(list(wanted))
    out, _, _ = p.handle(S["connected"], 1.0)
    assert out == [{"action": "auth", "key": "PKTESTKEY", "secret": "test-secret"}]
    out, _, _ = p.handle(S["authenticated"], 2.0)
    return p, out


def test_stream_handshake_subscribes_and_delivers_data():
    p, out = ready_protocol()
    assert out == [{"action": "subscribe", "trades": ["AAPL", "AMD"], "quotes": ["AAPL", "AMD"]}]
    assert p.phase == "ready" and p.streamed() == set()
    out, data, action = p.handle(S["subscription_aapl_amd"], 3.0)
    assert out == [] and action is None and p.streamed() == {"AAPL", "AMD"}
    out, data, action = p.handle(S["trade_aapl"] + S["quote_amd"] + S["bar_spy"], 4.0)
    assert [m["T"] for m in data] == ["t", "q"] and data[0]["p"] == D("126.55")
    # changing interest sends only the difference
    out = p.set_wanted(["AAPL", "TSLA"])
    assert out == [{"action": "unsubscribe", "trades": ["AMD"], "quotes": ["AMD"]},
                   {"action": "subscribe", "trades": ["TSLA"], "quotes": ["TSLA"]}]
    assert p.set_wanted(["AAPL", "TSLA", "SPY"]) == []  # waits for both replies first
    sub = [{"T": "subscription", "trades": ["AAPL"], "quotes": ["AAPL"]}]
    assert p.handle(sub, 5.0)[0] == []
    sub = [{"T": "subscription", "trades": ["AAPL", "TSLA"], "quotes": ["AAPL", "TSLA"]}]
    assert p.handle(sub, 6.0)[0] == [{"action": "subscribe", "trades": ["SPY"], "quotes": ["SPY"]}]


def test_stream_auth_failure_stops_and_explains():
    p = StreamProtocol("bad", "bad")
    p.connecting()
    p.handle(S["connected"], 1.0)
    out, data, action = p.handle(S["auth_failed"], 2.0)
    assert action == "stop" and p.fatal and p.phase == "failed"
    assert "APCA_API_KEY_ID" in p.error and "bad" not in p.error.replace("rejected", "")


def test_stream_insufficient_subscription_stops():
    p, _ = ready_protocol()
    assert p.handle(S["insufficient_subscription"], 3.0)[2] == "stop" and p.fatal


def test_stream_connection_limit_backs_off():
    p = StreamProtocol("k", "s")
    p.connecting()
    p.handle(S["connected"], 1.0)
    _, _, action = p.handle(S["connection_limit"], 2.0)
    assert action == "reconnect" and p.retry_after >= 60 and not p.fatal and "one stream connection" in p.error


def test_stream_symbol_limit_shrinks_the_cap_and_keeps_the_most_important():
    wanted = [f"S{i:02d}" for i in range(40)]
    p, out = ready_protocol(wanted, cap=30)
    assert len(out[0]["quotes"]) == 30
    # the server refused: nothing subscribed yet, so halve and retry with the 15 most important
    out, _, action = p.handle(S["symbol_limit"], 3.0)
    assert action is None and p.cap == 15 and out[0]["quotes"] == sorted(wanted[:15])
    p.handle([{"T": "subscription", "trades": wanted[:15], "quotes": wanted[:15]}], 4.0)
    # later another refusal while 15 are live: the cap becomes what the server accepted
    p.set_wanted(["NEW"] + wanted)
    out, _, _ = p.handle(S["symbol_limit"], 5.0)
    assert p.cap == 15 and "polling the rest" in p.error


class ScriptedConnection:
    def __init__(self, frames, sent):
        self.frames, self.sent = list(frames), sent

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout):
        if not self.frames:
            raise ConnectionError("closed by server")
        f = self.frames.pop(0)
        return None if f is None else json.dumps(f, default=str)

    def close(self):
        pass


def test_stream_client_reconnects_and_resubscribes():
    scripts = [
        [S["connected"], S["authenticated"], S["subscription_aapl_amd"], S["trade_aapl"]],  # then drops
        [S["connected"], S["authenticated"], None, S["subscription_aapl_amd"], S["quote_amd"]],
    ]
    sent, got = [[], []], []

    def connector(url):
        assert url == "wss://stream.data.alpaca.markets/v2/iex"
        i = len([s for s in sent if s]) if sent[0] else 0
        return ScriptedConnection(scripts.pop(0), sent[i])

    p = StreamProtocol("PKTESTKEY", "test-secret")
    c = StreamClient("wss://stream.data.alpaca.markets/v2/iex", p, got.extend, connector, sleep=lambda s: None)
    c.set_wanted(["AAPL", "AMD"])
    assert c.run_once() == "reconnect" and p.phase == "idle" and "disconnected" in p.error
    assert p.streamed() == set()  # after a drop nothing counts as streamed
    assert c.run_once() == "reconnect"
    for conn_sent in sent:
        assert conn_sent[0]["action"] == "auth"
        assert conn_sent[1] == {"action": "subscribe", "trades": ["AAPL", "AMD"], "quotes": ["AAPL", "AMD"]}
    assert [m["T"] for m in got] == ["t", "q"]


def test_stream_client_stops_after_auth_failure():
    sent = []
    p = StreamProtocol("k", "s")
    c = StreamClient("wss://x", p, lambda m: None, lambda url: ScriptedConnection([S["connected"], S["auth_failed"]], sent),
                     sleep=lambda s: None)
    c.run_forever()  # returns instead of hammering the server with a bad key
    assert p.fatal and len(sent) == 1


# ---- rate limiting and HTTP ---------------------------------------------------------------------------
class FakeMono:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_rate_limiter_spreads_requests():
    m = FakeMono()
    rl = RateLimiter(3, clock=m, sleep=m.sleep)
    for _ in range(3):
        rl.acquire()
    assert m.slept == []
    rl.acquire()
    assert m.slept == [pytest.approx(20.0)]
    with pytest.raises(RateLimited):
        rl.acquire(max_wait=5)  # a web request does not wait 20 s
    m.t += 60
    rl.acquire(max_wait=0)  # bucket refilled
    rl.penalize(30)
    with pytest.raises(RateLimited):
        rl.acquire(max_wait=10)


def test_http_client_backs_off_on_429_and_retries_5xx():
    m = FakeMono()
    tr = FakeTransport({"/x": [(429, {"message": "too many requests"}, {"X-RateLimit-Reset": "1003"}),
                               (500, "oops"), "{\"ok\": 1.25}"]})
    c = JsonClient("Alpaca", {"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"}, tr,
                   RateLimiter(100, clock=m, sleep=m.sleep), wall_clock=lambda: 1000.0, sleep=m.sleep)
    assert c.get("https://data.alpaca.markets", "/x", max_wait=10) == {"ok": D("1.25")}
    assert len(tr.requests) == 3 and 3.0 in m.slept
    tr = FakeTransport({"/x": (403, {"message": "forbidden"})})
    c = JsonClient("Alpaca", {}, tr, RateLimiter(100), sleep=lambda s: None)
    with pytest.raises(ProviderError) as e:
        c.get("https://data.alpaca.markets", "/x")
    assert e.value.status == 403 and "forbidden" in e.value.message and len(tr.requests) == 1
    tr = FakeTransport({})
    tr.get = lambda *a: (_ for _ in ()).throw(OSError("name resolution failed"))
    c = JsonClient("Alpaca", {}, tr, RateLimiter(100), sleep=lambda s: None)
    with pytest.raises(ProviderError) as e:
        c.get("https://data.alpaca.markets", "/x")
    assert e.value.status == 0 and "data.alpaca.markets" in e.value.message


def test_requests_authenticate_by_header_and_never_put_keys_in_urls():
    feed, tr, _ = make_feed()
    feed.load_assets()
    feed.rest.snapshots(["AAPL"], "iex")
    for r in tr.requests:
        assert r["headers"]["APCA-API-KEY-ID"] == "PKTESTKEY" and r["headers"]["APCA-API-SECRET-KEY"] == "test-secret"
        assert "PKTESTKEY" not in r["url"] and "test-secret" not in r["url"]
    assert {r["host"] for r in tr.requests} == {"paper-api.alpaca.markets", "data.alpaca.markets"}
    assert feed.status_info()["rest_error"] is None


def test_adapter_has_no_order_endpoint():
    src = (Path(al.__file__)).read_text(encoding="utf-8") + Path(al.__file__).with_name("http.py").read_text(encoding="utf-8")
    assert "/orders" not in src and "POST" not in src.replace('method="GET"', "")


# ---- the feed ----------------------------------------------------------------------------------------
def test_assets_load_filter_and_cache(tmp_path):
    feed, tr, _ = make_feed(data_dir=str(tmp_path))
    assert feed.load_assets() == 4  # the OTC, non-tradable row is dropped
    assert feed.symbols()["SPY"]["type"] == "etf" and feed.symbols()["AAPL"]["type"] == "stock"
    feed2, tr2, _ = make_feed(data_dir=str(tmp_path))
    assert feed2.load_assets() == 4 and tr2.requests == []  # served from the day-old cache


def test_poll_streams_the_most_important_and_polls_the_rest():
    feed, tr, _ = make_feed(stream_cap=2)
    feed.load_assets()
    feed.interest_source = lambda: ["TSLA", "AAPL", "AMD", "SPY", "NOPE"]
    feed.protocol.phase = "ready"
    feed.protocol.subscribed = {"TSLA", "AAPL"}
    feed.poll_once()
    snap = [r for r in tr.requests if r["path"] == "/v2/stocks/snapshots"]
    assert snap[0]["params"]["symbols"] == "TSLA,AAPL,AMD,SPY" and snap[0]["params"]["feed"] == "iex"
    assert feed._polled_count == 2
    quotes = {q.symbol: q for q in feed.step(0)}
    assert set(quotes) == {"AAPL", "TSLA"}  # the fixture only has these two
    tr.requests.clear()
    feed.wall_clock.t += 6
    feed.poll_once()  # streamed symbols wait 60 s, polled ones 5 s
    snap = [r for r in tr.requests if r["path"] == "/v2/stocks/snapshots"]
    assert snap[0]["params"]["symbols"] == "AMD,SPY"


def test_stream_trades_update_quotes_and_prints():
    feed, _, _ = make_feed(t="2021-02-22T16:00:00Z")
    feed._on_stream(S["quote_amd"] + [{"T": "t", "S": "AMD", "p": D("87.68"), "s": 300, "t": "2021-02-22T15:51:46Z"}])
    (q,) = feed.step(0)
    assert (q.bid, q.ask, q.last, q.last_size, q.volume) == (D("87.66"), D("87.68"), D("87.68"), D(300), D(300))
    assert q.trade and q.bid_size == D(100) and q.ask_size == D(400)
    assert feed.trades("AMD")[0]["side"] == "buy" and feed.step(1) == []


def test_bars_cache_and_request_shape():
    feed, tr, wall = make_feed(t="2022-01-03T15:00:00Z")
    rows = feed.bars("AAPL", "1m", 100)
    assert rows and rows[0]["close"] == 178.21
    req = tr.requests[-1]["params"]
    assert req["timeframe"] == "1Min" and req["feed"] == "iex" and req["sort"] == "desc" and req["limit"] == "100"
    feed.bars("AAPL", "1m", 100)
    assert len(tr.requests) == 1  # cached
    feed.bars("AAPL", "1d", 50)
    req = tr.requests[-1]["params"]
    assert req["timeframe"] == "1Day" and req["feed"] == "sip" and req["adjustment"] == "split" and "end" in req
    for tf, name in (("5m", "5Min"), ("15m", "15Min"), ("1h", "1Hour"), ("1w", "1Week")):
        feed.bars("AAPL", tf, 10)
        assert tr.requests[-1]["params"]["timeframe"] == name


def test_bars_ready_answers_from_memory_and_the_history_loader_fetches():
    """bars_ready is what code under the exchange lock uses: it never makes a request."""
    feed, tr, wall = make_feed(t="2022-01-03T15:00:00Z")
    assert feed.bars_ready("AAPL", "1m", 65) is None and tr.requests == []
    assert feed.bars_ready("AAPL", "1m", 4) is None  # the larger request stays
    assert feed.load_wanted_bars() == 1 and tr.requests[-1]["params"]["limit"] == "65"
    rows = feed.bars_ready("AAPL", "1m", 65)
    assert rows and rows[0]["close"] == 178.21 and feed.bars_ready("AAPL", "1m", 4) == rows[-4:]
    assert feed.load_wanted_bars() == 0 and len(tr.requests) == 1
    # a mark: only history read after it counts, however fresh the cache is
    mark = feed.history_mark()
    assert feed.bars_ready("AAPL", "1m", 4, mark) is None and len(tr.requests) == 1
    assert feed.load_wanted_bars() == 1
    assert tr.requests[-1]["params"]["limit"] == "65"  # the cache keeps its length for the chart
    assert feed.bars_ready("AAPL", "1m", 4, mark) == rows[-4:]
    assert feed.bars_ready("AAPL", "1m", 4, feed.history_mark()) is None
    feed.bars("AAPL", "1m", 100)  # a chart request made later satisfies the mark too
    assert feed.bars_ready("AAPL", "1m", 4, mark) is not None
    # without a mark the cache goes stale as it does for bars()
    feed.load_wanted_bars()
    wall.t += 31
    n = len(tr.requests)
    assert feed.bars_ready("AAPL", "1m", 4) is None and len(tr.requests) == n
    with pytest.raises(ValueError):
        feed.bars_ready("AAPL", "7m", 10)


def test_a_failed_history_load_is_tried_again_after_a_pause():
    down = [True]
    feed, tr, wall = make_feed(routes=routes(**{"/v2/stocks/bars": lambda req: (500, "down") if down[0]
                                                 else (200, raw("alpaca/stock_bars.json"))}),
                               t="2022-01-03T15:00:00Z")
    assert feed.bars_ready("AAPL", "1m", 10) is None
    assert feed.load_wanted_bars() == 1 and feed.bars_ready("AAPL", "1m", 10) is None
    assert feed.load_wanted_bars() == 0  # not hammered
    down[0] = False
    assert feed.load_wanted_bars() == 0
    wall.t += 10
    assert feed.load_wanted_bars() == 1 and feed.bars_ready("AAPL", "1m", 10)
    assert feed.load_wanted_bars() == 0


def test_the_history_thread_loads_what_was_asked_for():
    feed, tr, _ = make_feed(t="2022-01-03T15:00:00Z")
    t = threading.Thread(target=feed._history_forever, daemon=True)
    t.start()
    try:
        assert feed.bars_ready("AAPL", "1d", 31) is None
        deadline = time.monotonic() + 5
        while feed.bars_ready("AAPL", "1d", 31) is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert feed.bars_ready("AAPL", "1d", 31)
    finally:
        feed.close()
        t.join(5)
    assert not t.is_alive()


def test_daily_bars_fall_back_to_iex_when_sip_is_refused():
    def bars(req):
        if req["params"]["feed"] == "sip":
            return 403, {"message": "subscription does not permit querying recent SIP data"}
        return 200, raw("alpaca/stock_bars.json")
    feed, tr, _ = make_feed(routes=routes(**{"/v2/stocks/bars": bars}))
    assert feed.bars("AAPL", "1d", 10)
    assert [r["params"]["feed"] for r in tr.requests] == ["sip", "iex"]


def test_bars_failure_returns_empty_not_an_exception():
    feed, _, _ = make_feed(routes=routes(**{"/v2/stocks/bars": (500, "down")}))
    assert feed.bars("AAPL", "1d", 10) == []
    with pytest.raises(ValueError):
        feed.bars("AAPL", "7m", 10)


def contracts_for(expiry: str):
    j = json.loads(raw("alpaca/option_contracts.json"))
    c = j["option_contracts"][0]
    c.update(expiration_date=expiry, symbol="AAPL240426C00162500", underlying_symbol="AAPL", open_interest="237")
    return json.dumps(j)


def option_feed():
    feed, tr, wall = make_feed(t="2024-04-22T20:00:00Z",
                               routes=routes(**{"/v2/options/contracts": contracts_for("2024-04-26")}))
    feed.load_assets()
    return feed, tr, wall


def test_feed_option_chain_expirations_and_quotes():
    feed, tr, wall = option_feed()
    assert feed.option_expirations("AAPL") == [date(2024, 4, 26)]
    ch = feed.option_chain("AAPL", date(2024, 4, 26), wall())
    assert ch["source"] == "alpaca-indicative" and "indicative" in ch["source_label"] and ch["model_legs"] == 1
    assert ch["rows"][0]["call"]["open_interest"] == 237
    req = [r for r in tr.requests if r["path"] == "/v1beta1/options/snapshots/AAPL"][0]["params"]
    assert req["feed"] == "indicative" and req["expiration_date"] == "2024-04-26"
    q = feed.option_quote("AAPL240426C00162500")
    assert (q.bid, q.ask) == (D("4.15"), D("4.3"))
    assert feed.option_quote("AAPL240426P00162500") is None  # the provider has nothing: model applies
    wall.t += 1000
    assert feed.option_quote("AAPL240426C00162500") is None  # too old to trade against


def test_status_reports_source_and_staleness():
    feed, _, wall = make_feed(t="2024-04-22T15:00:00Z")  # a Monday, market open
    st = feed.status_info()
    assert st["realtime"] is True and st["data_exchange"] == "IEX" and st["data_source"] == "Alpaca"
    assert st["stale"] is True  # nothing has arrived yet
    feed.rest.snapshots(["AAPL"], "iex")
    assert feed.status_info()["stale"] is False
    delayed, _, _ = make_feed(feed="delayed_sip")
    assert delayed.delay_seconds == 900 and delayed.status_info()["realtime"] is False
    with pytest.raises(ValueError):
        make_feed(feed="nasdaq")


# ---- the exchange round trip ------------------------------------------------------------------------
def test_orders_fill_against_adapter_quotes():
    # The documentation snapshot is from 06:18 New York time; re-timed to 09:59:58 so it is a current
    # quote at the 10:00 test time (a 4-hour-old quote is stale and is not traded against).
    snaps = raw("alpaca/stock_snapshots.json").replace("2022-08-17T10:18:27.052763263Z", "2022-08-17T13:59:58Z")
    feed, tr, wall = make_feed(t="2022-08-17T14:00:00Z", routes=routes(**{"/v2/stocks/snapshots": snaps}))
    feed.load_assets()
    ex = Exchange(feed, SimClock(ts("2022-08-17T14:00:00Z")))
    acct = ex.create_account("Margin", "margin")
    from app.marketdata.config import wanted_symbols
    feed.interest_source = lambda: wanted_symbols(ex)
    ex.watchlists = {"Main": ["AAPL"]}
    feed.poll_once()
    ex.tick()
    assert ex.quote("AAPL").ask == D("172.7")
    (o,) = ex.submit(acct.id, {"symbol": "AAPL", "side": "buy", "qty": 10})
    assert o.status == "filled" and o.avg_fill_price == D("172.7")
    # a resting limit fills when a streamed quote crosses it
    (lim,) = ex.submit(acct.id, {"symbol": "AAPL", "side": "buy", "qty": 5, "type": "limit", "limit_price": "172.00"})
    assert lim.status == "working"
    assert "AAPL" in feed.wanted()
    feed._on_stream([{"T": "q", "S": "AAPL", "bp": D("171.90"), "bs": 3, "ap": D("171.95"), "as": 2,
                      "t": "2022-08-17T14:00:01Z"}])
    ex.clock.advance(1)
    ex.tick()
    assert ex.engine.orders[lim.id].status == "filled" and ex.engine.orders[lim.id].avg_fill_price == D("171.95")
    assert ex.account(acct.id).positions()["AAPL"] == D(15)


def test_option_order_fills_against_provider_quote():
    feed, tr, wall = option_feed()
    ex = Exchange(feed, SimClock(ts("2024-04-22T15:00:00Z")))
    acct = ex.create_account("Margin", "margin")
    feed.apply_snapshots(json.loads(raw("alpaca/stock_snapshots.json"), parse_float=D))
    ex.tick()
    feed.option_chain("AAPL", date(2024, 4, 26), ex.now())
    (o,) = ex.submit(acct.id, {"symbol": "AAPL240426C00162500", "side": "buy", "qty": 1})
    assert o.status == "filled" and o.avg_fill_price == D("4.3")
    # a contract the provider does not quote still trades on the model price
    model = ex.quote("AAPL240426P00162500")
    assert model is not None and model.ask > model.bid
