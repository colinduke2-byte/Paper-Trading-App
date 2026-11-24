"""Discovery: watchlist sets, search, screener, market overview and compare (no network)."""
import json
import math
import shutil
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

from app.api.server import create_app
from app.core.clock import SimClock
from app.core.models import Quote
from app.discovery import screener as sc
from app.discovery import stats
from app.discovery.store import COLUMNS, MAX_LISTS, MAX_SYMBOLS
from app.exchange import Exchange
from app.marketdata.feed import SimFeed
from app.marketdata.synthetic import SyntheticFeed

D = Decimal
T0 = 1795500000.0  # 2026-11-23 03:20 UTC
DAY = 86400
FIX = Path(__file__).parent / "fixtures"


def synthetic_client(tmp_path=None, watch=None):
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()), data_dir=str(tmp_path) if tmp_path else None)
    ex.create_account("Margin", "margin")
    ex.watchlists = watch if watch is not None else {"Main": ["NVLX", "ORBT", "MKTX"]}
    ex.tick()
    return TestClient(create_app(ex, run_loop=False)), ex


# ---- a fixed dataset ---------------------------------------------------------------------------------
def daily(closes, volume=1000.0, end_day=None):
    """Daily bars ending the day before T0, high/low 1 above/below the close."""
    end_day = end_day if end_day is not None else int(T0 // DAY) - 1
    n = len(closes)
    return [[(end_day - (n - 1 - i)) * DAY, c, c + 1, c - 1, c, volume] for i, c in enumerate(closes)]


def put_quote(feed, sym, last, prev, open_=None, volume=0, high=None, low=None):
    last, prev = D(str(last)), D(str(prev))
    o = D(str(open_)) if open_ is not None else last
    feed.quotes[sym] = Quote(symbol=sym, bid=last - D("0.01"), ask=last, bid_size=D(100), ask_size=D(100), last=last,
                             volume=D(volume), ts=T0, open=o, high=D(str(high)) if high else max(o, last),
                             low=D(str(low)) if low else min(o, last), prev_close=prev)


def fixed_feed():
    info = {
        "UPP": {"name": "Upward Industries", "sector": "Industrials", "type": "stock", "shares_outstanding": 1_000_000,
                "eps": 5.0, "dividend": 0.0},
        "DWN": {"name": "Downward Holdings", "sector": "Energy", "type": "stock", "shares_outstanding": 50_000_000,
                "eps": -1.0, "dividend": 2.45},
        "FLAT": {"name": "Flatline Utilities", "sector": "Utilities", "type": "stock", "shares_outstanding": 10_000_000,
                 "eps": 2.0, "dividend": 1.0},
        "FUND": {"name": "Everything Fund", "sector": "ETF", "type": "etf", "shares_outstanding": 2_000_000, "dividend": 0.4},
        "NOH": {"name": "No History Corp", "sector": "Industrials", "type": "stock"},
    }
    feed = SimFeed({s: dict(i, symbol=s, vol=0.3) for s, i in info.items()})
    feed.store.daily["UPP"] = daily([50 + i * 50 / 259 for i in range(260)])            # steady rise to 100
    feed.store.daily["DWN"] = daily([100 - i * 50 / 259 for i in range(260)], volume=5000)  # steady fall to 50
    feed.store.daily["FLAT"] = daily([20.0] * 260)
    feed.store.daily["FUND"] = daily([40 + (i % 2) for i in range(260)])
    put_quote(feed, "UPP", 101, 100, open_=101, volume=4000)   # gap +1 %, rel volume 4
    put_quote(feed, "DWN", 49, 50, open_=49, volume=1000)      # gap -2 %, rel volume 0.2
    put_quote(feed, "FLAT", 20, 20, volume=1000)
    put_quote(feed, "FUND", 41, 40, open_=40.5, volume=500)
    put_quote(feed, "NOH", 10, 9.5, open_=9.5, volume=10)
    return feed


@pytest.fixture
def fixed():
    feed = fixed_feed()
    ex = Exchange(feed, SimClock(T0))
    ex.create_account("Margin", "margin")
    ex.watchlists = {"Main": ["UPP", "DWN"]}
    return TestClient(create_app(ex, run_loop=False)), ex


def run(c, filters, **kw):
    r = c.post("/api/screener/run", json={"filters": filters, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def syms(res):
    return sorted(r["symbol"] for r in res["rows"])


# ---- watchlist sets ------------------------------------------------------------------------------------
def test_watchlist_sets_crud_and_order():
    c, ex = synthetic_client()
    j = c.get("/api/watchlist-sets").json()
    assert [l["name"] for l in j["lists"]] == ["Main"] and j["lists"][0]["layout"]["columns"] == ["last", "change", "change_pct"]
    assert {x["key"] for x in j["columns"]} == set(COLUMNS)
    j = c.post("/api/watchlist-sets", json={"name": "Tech", "symbols": ["pxlr", "ZZZZ", "PXLR", "sync"]}).json()
    assert [l["name"] for l in j["lists"]] == ["Main", "Tech"] and j["lists"][1]["symbols"] == ["PXLR", "SYNC"]
    # duplicate keeps the layout and lands next to its source
    c.patch("/api/watchlist-sets/Tech", json={"columns": ["last", "volume"], "density": "compact"})
    j = c.post("/api/watchlist-sets/Tech/duplicate", json={"name": "Tech 2"}).json()
    assert [l["name"] for l in j["lists"]] == ["Main", "Tech", "Tech 2"]
    assert j["lists"][2]["layout"]["columns"] == ["last", "volume"] and j["lists"][2]["symbols"] == ["PXLR", "SYNC"]
    j = c.post("/api/watchlist-sets/main/duplicate", json={}).json()  # names match ignoring case
    assert [l["name"] for l in j["lists"]][:2] == ["Main", "Main copy"]
    # rename keeps position, symbols and layout
    j = c.patch("/api/watchlist-sets/Tech", json={"name": "Chips"}).json()
    assert [l["name"] for l in j["lists"]] == ["Main", "Main copy", "Chips", "Tech 2"]
    assert j["lists"][2]["layout"]["density"] == "compact"
    # reorder lists and symbols
    j = c.put("/api/watchlist-order", json=["Tech 2", "Chips", "Main", "Main copy"]).json()
    assert [l["name"] for l in j["lists"]] == ["Tech 2", "Chips", "Main", "Main copy"]
    assert list(ex.watchlists) == ["Tech 2", "Chips", "Main", "Main copy"]
    j = c.patch("/api/watchlist-sets/Chips", json={"symbols": ["SYNC", "PXLR", "NVLX"]}).json()
    assert j["lists"][1]["symbols"] == ["SYNC", "PXLR", "NVLX"]
    j = c.delete("/api/watchlist-sets/Tech 2").json()
    assert [l["name"] for l in j["lists"]] == ["Chips", "Main", "Main copy"]
    # the legacy routes see the same lists
    assert c.get("/api/watchlists").json()["Chips"] == ["SYNC", "PXLR", "NVLX"]
    c.put("/api/watchlists/Chips", json=["ORBT"])
    assert c.get("/api/watchlist-sets").json()["lists"][0]["symbols"] == ["ORBT"]


def test_watchlist_validation_and_bounds():
    c, ex = synthetic_client()
    bad = lambda r, text: r.status_code == 400 and text in r.json()["detail"]
    assert bad(c.post("/api/watchlist-sets", json={"name": "  "}), "cannot be empty")
    assert bad(c.post("/api/watchlist-sets", json={"name": "x" * 31}), "at most 30")
    assert bad(c.post("/api/watchlist-sets", json={"name": "a/b"}), "slashes")
    assert bad(c.post("/api/watchlist-sets", json={"name": "MAIN"}), "already exists")
    assert bad(c.post("/api/watchlist-sets", json={"name": "Z", "symbols": "NVLX"}), "must be a list")
    assert c.patch("/api/watchlist-sets/Nope", json={"name": "X"}).status_code == 404
    assert c.delete("/api/watchlist-sets/Nope").status_code == 404
    assert c.get("/api/watchlist-sets/Nope/rows").status_code == 404
    assert bad(c.delete("/api/watchlist-sets/Main"), "at least one")
    assert bad(c.patch("/api/watchlist-sets/Main", json={"columns": ["last", "colour"]}), "unknown column colour")
    assert bad(c.patch("/api/watchlist-sets/Main", json={"density": "tiny"}), "density")
    assert bad(c.patch("/api/watchlist-sets/Main", json={"sort": {"key": "nope"}}), "sort key")
    assert bad(c.patch("/api/watchlist-sets/Main", json={"colour": "red"}), "unknown field colour")
    assert bad(c.patch("/api/watchlist-sets/Main", json={"symbols": 5}), "must be a list")
    c.post("/api/watchlist-sets", json={"name": "Two"})
    assert bad(c.patch("/api/watchlist-sets/Two", json={"name": "main"}), "already exists")
    assert c.patch("/api/watchlist-sets/Two", json={"name": "TWO"}).status_code == 200  # a change of case is fine
    assert bad(c.put("/api/watchlist-order", json=["Main"]), "every watchlist")
    assert bad(c.put("/api/watchlist-order", json=["Main", "Main"]), "every watchlist")
    assert c.put("/api/watchlist-order", json=["Main", "Nope"]).status_code == 404
    assert bad(c.get("/api/watchlist-sets/Main/rows", params={"columns": "last,nope"}), "unknown column nope")
    for i in range(MAX_LISTS - 2):
        assert c.post("/api/watchlist-sets", json={"name": f"L{i}"}).status_code == 200
    assert bad(c.post("/api/watchlist-sets", json={"name": "One too many"}), f"at most {MAX_LISTS}")
    assert bad(c.post("/api/watchlist-sets/Main/duplicate", json={"name": "Copy"}), f"at most {MAX_LISTS}")


def test_adding_symbols_respects_the_list_limit():
    feed = SimFeed({f"S{i:03d}": {"symbol": f"S{i:03d}", "name": f"Stock {i}", "sector": "Test", "vol": 0.3}
                    for i in range(150)})
    ex = Exchange(feed, SimClock(T0))
    ex.watchlists = {"Main": ["S000", "S001"]}
    c = TestClient(create_app(ex, run_loop=False))
    r = c.post("/api/watchlist-sets/Main/symbols", json={"symbols": ["s001", "S002", "NOPE"], "at": "start"}).json()
    assert r["added"] == ["S002"] and r["already"] == ["S001"] and r["unknown"] == ["NOPE"]
    assert r["list"]["symbols"] == ["S002", "S000", "S001"]
    r = c.post("/api/watchlist-sets/Main/symbols", json={"symbols": [f"S{i:03d}" for i in range(150)]}).json()
    assert len(r["list"]["symbols"]) == MAX_SYMBOLS and len(r["full"]) == 150 - MAX_SYMBOLS
    r = c.patch("/api/watchlist-sets/Main", json={"symbols": [f"S{i:03d}" for i in range(101)]})
    assert r.status_code == 400 and "at most 100" in r.json()["detail"]


def test_rows_carry_columns_positions_and_sources():
    c, ex = synthetic_client()
    aid = list(ex.accounts)[0]
    ex.submit(aid, {"symbol": "NVLX", "side": "buy", "qty": 10})
    cols = ",".join(COLUMNS)
    j = c.get("/api/watchlist-sets/Main/rows", params={"columns": cols, "account": aid}).json()
    assert j["columns"] == list(COLUMNS) and [r["symbol"] for r in j["rows"]] == ["NVLX", "ORBT", "MKTX"]
    n = j["rows"][0]
    assert n["position_qty"] == 10 and n["unrealized_pnl"] is not None and n["quote_source"] == "simulated"
    assert 0 <= n["range_52w_pos"] <= 100 and n["low_52w"] <= n["last"] <= n["high_52w"]
    assert n["market_cap"] > 0 and n["pe"] > 0 and 2 <= len(n["sparkline"]) <= 40
    assert j["rows"][1]["position_qty"] is None
    assert n["change"] == pytest.approx(n["last"] - n["prev_close"])
    # without history columns no history is computed
    j = c.get("/api/watchlist-sets/Main/rows", params={"columns": "last"}).json()
    assert "range_52w_pos" not in j["rows"][0] and "sparkline" not in j["rows"][0]


# ---- persistence ---------------------------------------------------------------------------------------
def test_lists_layouts_and_screens_survive_a_restart(tmp_path):
    c, ex = synthetic_client(tmp_path)
    c.post("/api/watchlist-sets", json={"name": "Second", "symbols": ["AUTO"], "layout": {"columns": ["last", "sparkline"]}})
    c.patch("/api/watchlist-sets/Main", json={"sort": {"key": "change_pct", "desc": True}, "density": "compact"})
    c.put("/api/watchlist-order", json=["Second", "Main"])
    sid = c.post("/api/screens", json={"name": "Cheap", "filters": [{"field": "last", "max": 100}], "sort": "last"}).json()["id"]
    c.post("/api/search/recent", json={"symbol": "ORBT"})
    ex.save(force=True)
    ex2 = Exchange(SyntheticFeed(seed=3, now=time.time()), data_dir=str(tmp_path))
    assert ex2.load()
    c2 = TestClient(create_app(ex2, run_loop=False))
    lists = c2.get("/api/watchlist-sets").json()["lists"]
    assert [l["name"] for l in lists] == ["Second", "Main"]
    assert lists[0]["layout"]["columns"] == ["last", "sparkline"] and lists[0]["symbols"] == ["AUTO"]
    assert lists[1]["layout"] == {"columns": ["last", "change", "change_pct"], "density": "compact",
                                  "sort": {"key": "change_pct", "desc": True}}
    assert [s["id"] for s in c2.get("/api/screens").json()["saved"]] == [sid]
    assert [r["symbol"] for r in c2.get("/api/search/recent").json()] == ["ORBT"]
    raw = json.loads((tmp_path / "discovery.json").read_text(encoding="utf-8"))
    assert raw["version"] == 1 and set(raw["layouts"]) == {"Main", "Second"}


def test_old_state_with_a_single_main_list_loads(tmp_path):
    """A state.json from before named lists: {"Main": [...]} and no discovery.json."""
    shutil.copytree(FIX / "state_main", tmp_path, dirs_exist_ok=True)
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    state["watchlists"] = {"Main": ["ABC"]}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    feed = SimFeed()
    feed.set_quote("ABC", "99.99", "100.00")
    ex = Exchange(feed, SimClock(T0), data_dir=str(tmp_path))
    assert ex.load()
    c = TestClient(create_app(ex, run_loop=False))
    lists = c.get("/api/watchlist-sets").json()["lists"]
    assert lists == [{"name": "Main", "symbols": ["ABC"], "layout": {"columns": ["last", "change", "change_pct"],
                                                                     "density": "comfortable", "sort": None}}]
    assert c.get("/api/watchlists").json() == {"Main": ["ABC"]}
    assert c.get("/api/watchlist-sets/Main/rows").json()["rows"][0]["last"] == 100.0


def test_empty_or_damaged_discovery_state_falls_back_to_defaults(tmp_path):
    (tmp_path / "discovery.json").write_text("{not json", encoding="utf-8")
    ex = Exchange(SimFeed(), SimClock(T0), data_dir=str(tmp_path))
    ex.watchlists = {}
    c = TestClient(create_app(ex, run_loop=False))
    assert [l["name"] for l in c.get("/api/watchlist-sets").json()["lists"]] == ["Main"]  # an empty state gets one list
    (tmp_path / "discovery.json").write_text(json.dumps({"layouts": {"Main": {"columns": ["bogus"]}}}), encoding="utf-8")
    ex2 = Exchange(SimFeed(), SimClock(T0), data_dir=str(tmp_path))
    ex2.watchlists = {"Main": []}
    c2 = TestClient(create_app(ex2, run_loop=False))
    assert c2.get("/api/watchlist-sets").json()["lists"][0]["layout"]["columns"] == ["last", "change", "change_pct"]


# ---- search ---------------------------------------------------------------------------------------------
def test_search_ranks_ticker_then_name():
    c, _ = synthetic_client()
    r = c.get("/api/search", params={"q": "orbt"}).json()
    assert r[0]["symbol"] == "ORBT" and r[0]["match"] == "symbol"
    r = c.get("/api/search", params={"q": "or"}).json()
    assert r[0]["symbol"] == "ORBT"  # ticker prefix before names containing "or"
    r = c.get("/api/search", params={"q": "orbit cl"}).json()
    assert [x["symbol"] for x in r] == ["ORBT"]
    r = c.get("/api/search", params={"q": "fund", "types": "etf"}).json()
    assert {x["symbol"] for x in r} == {"MKTX", "TECX"} and all(x["type"] == "etf" for x in r)
    assert c.get("/api/search", params={"q": ""}).json() == []
    assert len(c.get("/api/search", params={"q": "e", "limit": 3}).json()) == 3
    assert c.get("/api/search", params={"q": "e", "limit": 0}).status_code == 422
    assert c.get("/api/search", params={"q": "e", "limit": 51}).status_code == 422
    assert c.get("/api/search", params={"q": "x" * 61}).status_code == 422
    assert c.get("/api/search", params={"q": "e", "types": "bond"}).status_code == 400


def test_search_is_fast_and_bounded_with_eleven_thousand_symbols():
    info = {}
    for i in range(11_000):
        s = "".join(chr(65 + (i // 26 ** k) % 26) for k in range(4))
        info[s] = {"symbol": s, "name": f"Company {s} Holdings", "type": "etf" if i % 10 == 0 else "stock"}
    ex = Exchange(SimFeed(info), SimClock(T0))
    c = TestClient(create_app(ex, run_loop=False))
    c.get("/api/search", params={"q": "A"})  # builds the index
    t = time.perf_counter()
    for q in ("AB", "holdings", "zz", "company q"):
        r = c.get("/api/search", params={"q": q, "limit": 50}).json()
        assert len(r) <= 50
    assert (time.perf_counter() - t) / 4 < 0.25
    r = c.get("/api/search", params={"q": "ABAA"}).json()
    assert r[0]["symbol"] == "ABAA"


def test_recent_symbols():
    c, _ = synthetic_client()
    for s in ("NVLX", "ORBT", "nvlx"):
        c.post("/api/search/recent", json={"symbol": s})
    assert [r["symbol"] for r in c.get("/api/search/recent").json()] == ["NVLX", "ORBT"]
    assert c.post("/api/search/recent", json={"symbol": "ZZZZ"}).status_code == 404
    for i, s in enumerate(list(c.app.state.exchange.feed.symbols())[:15]):
        c.post("/api/search/recent", json={"symbol": s})
    assert len(c.get("/api/search/recent").json()) == 12


# ---- screener ------------------------------------------------------------------------------------------
def test_screener_filters_on_a_fixed_dataset(fixed):
    c, _ = fixed
    every = run(c, [])
    assert every["screened"] == 5 and every["matched"] == 5 and every["universe"]["complete"] is True
    assert syms(run(c, [{"field": "last", "min": 20, "max": 50}])) == ["DWN", "FLAT", "FUND"]
    assert syms(run(c, [{"field": "change_pct", "min": 0.5}])) == ["FUND", "NOH", "UPP"]
    assert syms(run(c, [{"field": "change_pct", "max": -1}])) == ["DWN"]
    assert syms(run(c, [{"field": "volume", "min": 1000}])) == ["DWN", "FLAT", "UPP"]
    assert syms(run(c, [{"field": "rel_volume", "min": 3}])) == ["UPP"]          # 4000 / 1000
    assert syms(run(c, [{"field": "rel_volume", "max": 0.4}])) == ["DWN"]       # 1000 / 5000
    assert syms(run(c, [{"field": "market_cap", "min": 1e9}])) == ["DWN"]       # 49 x 50 M
    assert syms(run(c, [{"field": "market_cap", "max": 2e8}])) == ["FLAT", "FUND", "UPP"]
    assert syms(run(c, [{"field": "pe", "max": 15}])) == ["FLAT"]               # 10 (UPP 20.2, DWN loss)
    assert syms(run(c, [{"field": "dividend_yield", "min": 4}])) == ["DWN", "FLAT"]  # 5 % each
    assert syms(run(c, [{"field": "pct_from_high_52w", "max": 1}])) == ["UPP"]  # at its high
    assert syms(run(c, [{"field": "pct_from_low_52w", "max": 1}])) == ["DWN"]
    assert syms(run(c, [{"field": "gap_pct", "min": 0.5}])) == ["FUND", "UPP"]
    assert syms(run(c, [{"field": "gap_pct", "max": -1}])) == ["DWN"]
    assert syms(run(c, [{"field": "pct_vs_sma50", "min": 0.01}, {"field": "pct_vs_sma200", "min": 0.01}])) == ["FUND", "UPP"]
    assert syms(run(c, [{"field": "pct_vs_sma200", "max": -0.01}])) == ["DWN"]
    assert syms(run(c, [{"field": "rsi14", "min": 70}])) == ["UPP"]
    assert syms(run(c, [{"field": "rsi14", "max": 30}])) == ["DWN"]
    assert syms(run(c, [{"field": "rsi14", "min": 40, "max": 60}])) == ["FLAT", "FUND"]
    assert syms(run(c, [{"field": "type", "in": ["etf"]}])) == ["FUND"]
    assert syms(run(c, [{"field": "sector", "in": ["industrials"]}])) == ["NOH", "UPP"]
    assert syms(run(c, [{"field": "has_options", "equals": True}])) == ["DWN", "FLAT", "FUND", "NOH", "UPP"]
    assert syms(run(c, [{"field": "has_options", "equals": False}])) == []
    # a symbol without history never matches a history filter (it is not guessed)
    assert "NOH" not in syms(run(c, [{"field": "rsi14", "min": 0}]))
    # several filters are combined with AND
    assert syms(run(c, [{"field": "type", "in": ["stock"]}, {"field": "change_pct", "min": 0.5}])) == ["NOH", "UPP"]


def test_screener_values_match_independent_formulas(fixed):
    c, _ = fixed
    rows = {r["symbol"]: r for r in run(c, [])["rows"]}
    upp, flat = rows["UPP"], rows["FLAT"]
    assert upp["rsi14"] == 100 and rows["DWN"]["rsi14"] == pytest.approx(0) and flat["rsi14"] == 50
    closes = [50 + i * 50 / 259 for i in range(260)] + [101]
    assert upp["sma50"] == pytest.approx(sum(closes[-50:]) / 50)
    assert upp["sma200"] == pytest.approx(sum(closes[-200:]) / 200)
    assert upp["high_52w"] == pytest.approx(101) and upp["range_52w_pos"] == pytest.approx(100)
    assert upp["gap_pct"] == pytest.approx(1.0) and upp["rel_volume"] == pytest.approx(4.0)
    assert flat["dividend_yield"] == pytest.approx(5.0) and flat["pe"] == pytest.approx(10.0)
    assert rows["NOH"]["history"] is False and rows["NOH"].get("rsi14") is None


def test_screener_sorting_limit_and_validation(fixed):
    c, _ = fixed
    r = run(c, [], sort="change_pct", desc=True)
    vals = [x["change_pct"] for x in r["rows"]]
    assert vals == sorted(vals, reverse=True)
    r = run(c, [], sort="rsi14", desc=False, limit=2)
    assert [x["symbol"] for x in r["rows"]] == ["DWN", "FLAT"] and r["matched"] == 5
    assert [x["symbol"] for x in run(c, [], sort="rsi14", desc=True)["rows"]][-1] == "NOH"  # missing values go last
    assert [x["symbol"] for x in run(c, [], sort="symbol", desc=False)["rows"]] == ["DWN", "FLAT", "FUND", "NOH", "UPP"]
    bad = lambda body, text: (lambda r: r.status_code == 400 and text in r.json()["detail"])(c.post("/api/screener/run", json=body))
    assert bad({"filters": [{"field": "colour", "min": 1}]}, "unknown screener field colour")
    assert bad({"filters": [{"field": "last"}]}, "needs a minimum or a maximum")
    assert bad({"filters": [{"field": "last", "min": 5, "max": 1}]}, "minimum is above its maximum")
    assert bad({"filters": [{"field": "last", "min": "cheap"}]}, "must be a number")
    assert bad({"filters": [{"field": "last", "min": True}]}, "must be a number")
    assert bad({"filters": [{"field": "type", "in": "etf"}]}, "list of values")
    assert bad({"filters": [{"field": "has_options", "equals": "yes"}]}, "true or false")
    assert bad({"filters": [{"field": "last", "min": 1}] * 21}, "at most 20")
    assert bad({"filters": {}}, "must be a list")
    assert bad({"limit": 0}, "limit") and bad({"limit": 501}, "limit") and bad({"limit": "10"}, "limit")
    assert bad({"sort": "colour"}, "cannot sort by colour")
    r = c.post("/api/screener/run", content='{"filters": [{"field": "last", "min": NaN}]}',
               headers={"Content-Type": "application/json"})
    assert r.status_code in (400, 422)


def test_screener_fields_and_presets(fixed):
    c, _ = fixed
    j = c.get("/api/screener/fields").json()
    keys = {f["key"] for f in j["fields"]}
    assert {"rsi14", "pct_vs_sma50", "gap_pct", "rel_volume", "market_cap", "type", "sector", "has_options"} <= keys
    assert all(f["available"] for f in j["fields"])
    assert next(f for f in j["fields"] if f["key"] == "sector")["choices"] == ["ETF", "Energy", "Industrials", "Utilities"]
    for p in j["presets"]:
        assert run(c, p["filters"], sort=p["sort"], desc=p["desc"])["universe"]["label"] == "All 5 simulated symbols"
    assert j["universe"]["with_history"] == 5


def test_saved_screens_crud(fixed):
    c, _ = fixed
    body = {"name": "Uptrend", "filters": [{"field": "pct_vs_sma50", "min": 0.01}], "sort": "change_pct", "desc": True,
            "columns": ["last", "rsi14"]}
    s = c.post("/api/screens", json=body).json()
    assert s["id"] and s["filters"] == [{"field": "pct_vs_sma50", "min": 0.01, "max": None}]
    assert c.post("/api/screens", json=dict(body, name="uptrend")).status_code == 400  # names are unique
    assert c.post("/api/screens", json=dict(body, filters=[{"field": "nope", "min": 1}])).status_code == 400
    assert c.post("/api/screens", json=dict(body, columns=["nope"])).status_code == 400
    assert c.post("/api/screens", json=dict(body, name="")).status_code == 400
    u = c.put(f"/api/screens/{s['id']}", json=dict(body, name="Uptrend 2")).json()
    assert u["name"] == "Uptrend 2" and u["id"] == s["id"]
    saved = c.get("/api/screens").json()
    assert [x["name"] for x in saved["saved"]] == ["Uptrend 2"] and saved["presets"]
    assert syms(run(c, u["filters"], sort=u["sort"])) == ["FUND", "UPP"]
    assert c.put("/api/screens/nope", json=body).status_code == 404
    assert c.delete(f"/api/screens/{s['id']}").json()["ok"]
    assert c.delete(f"/api/screens/{s['id']}").status_code == 404


@settings(max_examples=60, deadline=None)
@given(st.lists(st.one_of(st.none(), st.floats(-1e6, 1e6)), min_size=0, max_size=30),
       st.one_of(st.none(), st.floats(-1e6, 1e6)), st.one_of(st.none(), st.floats(-1e6, 1e6)))
def test_numeric_filter_property(values, lo, hi):
    if lo is None and hi is None:
        lo = 0.0
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    rows = [{"symbol": f"S{i:02d}", "last": v} for i, v in enumerate(values)]
    (f,) = sc.check_filters([{"field": "last", "min": lo, "max": hi}])
    got = {r["symbol"] for r in sc.evaluate(rows, [f])}
    want = {r["symbol"] for r in rows if r["last"] is not None and (lo is None or r["last"] >= lo) and (hi is None or r["last"] <= hi)}
    assert got == want
    out = sc.sort_rows(rows, "last", True)
    present = [r["last"] for r in out if r["last"] is not None]
    assert present == sorted(present, reverse=True) and len(out) == len(rows)
    assert all(r["last"] is None for r in out[len(present):])


# ---- market overview -------------------------------------------------------------------------------------
def test_market_overview_on_the_simulated_feed():
    c, ex = synthetic_client()
    o = c.get("/api/markets/overview").json()
    assert o["simulated"] is True and o["sizing"] == "market_cap" and o["universe"]["total"] == 26
    g = [r["change_pct"] for r in o["gainers"]]
    l = [r["change_pct"] for r in o["losers"]]
    assert len(g) == 10 and g == sorted(g, reverse=True) and l == sorted(l) and g[0] >= l[0]
    assert all(r["type"] == "stock" for r in o["gainers"] + o["losers"])
    dv = [r["dollar_volume"] for r in o["active"]]
    assert dv == sorted(dv, reverse=True)
    assert {t["symbol"] for t in o["indices"]} == {"MKTX", "TECX"} and all(t["sparkline"] for t in o["indices"])
    b = o["breadth"]
    assert b["advancers"] + b["decliners"] + b["unchanged"] == b["total"] == 26
    assert 0 <= b["above_sma50"] <= b["with_history"] == 26
    sectors = {s["sector"]: s for s in o["sectors"]}
    assert "ETF" not in sectors and sum(s["count"] for s in o["sectors"]) == 24
    tech = sectors["Technology"]
    w = sum(m["size"] * m["change_pct"] for m in tech["members"]) / sum(m["size"] for m in tech["members"])
    assert tech["change_pct"] == pytest.approx(w) and tech["size"] == pytest.approx(sum(m["size"] for m in tech["members"]))
    sizes = [s["size"] for s in o["sectors"]]
    assert sizes == sorted(sizes, reverse=True)
    assert c.get("/api/markets/overview", params={"limit": 3}).json()["gainers"].__len__() == 3
    assert c.get("/api/markets/overview", params={"limit": 26}).status_code == 422
    assert c.get("/api/markets/overview", params={"limit": 0}).status_code == 422


# ---- compare -------------------------------------------------------------------------------------------
def test_compare_performance_and_bounds(fixed):
    c, _ = fixed
    j = c.get("/api/compare", params={"symbols": "upp,flat"}).json()
    assert [s["symbol"] for s in j["symbols"]] == ["UPP", "FLAT"] and j["periods"] == ["1D", "1W", "1M", "3M", "1Y"]
    upp, flat = j["symbols"]
    closes = [50 + i * 50 / 259 for i in range(260)]
    # one week back is 7 calendar days before today: the bar 7 days before the last one (daily bars here are consecutive days)
    assert upp["performance"]["1D"] == pytest.approx(1.0)
    assert upp["performance"]["1W"] == pytest.approx((101 / closes[-7] - 1) * 100)
    assert upp["performance"]["1Y"] is None  # 260 calendar days of history is less than a year
    assert flat["performance"]["3M"] == pytest.approx(0) and flat["pe"] == pytest.approx(10)
    assert c.get("/api/compare", params={"symbols": "UPP"}).status_code == 400
    assert c.get("/api/compare", params={"symbols": "UPP,upp"}).status_code == 400
    assert c.get("/api/compare", params={"symbols": "UPP,DWN,FLAT,FUND,NOH"}).status_code == 200
    six = ",".join(["UPP", "DWN", "FLAT", "FUND", "NOH", "ZZZ"])
    assert c.get("/api/compare", params={"symbols": six}).status_code == 400
    assert c.get("/api/compare", params={"symbols": "UPP,ZZZZ"}).status_code == 404
    noh = c.get("/api/compare", params={"symbols": "UPP,NOH"}).json()["symbols"][1]
    assert noh["performance"]["1D"] == pytest.approx((10 / 9.5 - 1) * 100) and noh["performance"]["1W"] is None


def test_stats_helpers():
    assert stats.rsi([1, 2, 3]) is None and stats.rsi(list(range(20))) == 100 and stats.rsi([5.0] * 20) == 50
    assert stats.sma([1, 2, 3, 4], 2) == 3.5 and stats.sma([1], 2) is None
    assert stats.sparkline([{"close": float(i)} for i in range(100)], 10)[-1] == 99.0
    assert len(stats.sparkline([{"close": float(i)} for i in range(100)], 10)) == 10
    assert stats.history_stats([], 10.0) == {"history_days": 0}
    h = stats.history_stats([{"time": 0, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 10}] * 30, 2.0, volume=20)
    assert h["rel_volume"] == 2 and h["range_52w_pos"] == 100 and h["volatility"] is not None
    assert math.isclose(stats.pct(110, 100), 10)


# ---- the live feed: rate limit and stream interest ----------------------------------------------------------
from test_alpaca import FakeTransport, Wall, fixture, raw, ts  # noqa: E402
from app.marketdata.alpaca import AlpacaFeed  # noqa: E402
from app.discovery import data as dd  # noqa: E402


def _ticker(i):
    return "".join(chr(65 + (i // 26 ** k) % 26) for k in range(4))


def live_app(n=11_000):
    snap = fixture("alpaca/stock_snapshots.json")["AAPL"]
    bars = json.loads(raw("alpaca/stock_bars.json"))["bars"]["AAPL"]
    assets = [{"symbol": s, "name": f"{s} Inc", "exchange": "NASDAQ", "tradable": True, "status": "active", "class": "us_equity"}
              for s in ["SPY", "QQQ", "XLK", "XLF"] + [_ticker(i) for i in range(n - 4)]]

    def snapshots(req):
        return json.dumps({s: snap for s in req["params"]["symbols"].split(",")}, default=str)

    def bar_route(req):
        return json.dumps({"bars": {req["params"]["symbols"]: bars}, "next_page_token": None})

    tr = FakeTransport({"/v2/assets": json.dumps(assets), "/v2/stocks/snapshots": snapshots, "/v2/stocks/bars": bar_route})
    wall = Wall(ts("2022-08-16T15:00:00Z"))
    feed = AlpacaFeed("k", "s", transport=tr, sleep=lambda s: None, wall_clock=wall)
    feed.load_assets()
    ex = Exchange(feed, SimClock(ts("2022-08-16T15:00:00Z")), session_mode="us")
    ex.create_account("Margin", "margin")
    ex.watchlists = {"Main": ["SPY"]}
    return TestClient(create_app(ex, run_loop=False)), ex, feed, tr, wall


def calls(tr, path, since=0):
    return sum(1 for r in tr.requests[since:] if r["path"] == path)


def test_live_screener_respects_the_request_budget():
    c, ex, feed, tr, wall = live_app()
    assert len(feed.symbols()) == 11_000
    start = len(tr.requests)
    seen = []
    for minute in range(5):
        before = len(tr.requests)
        for _ in range(30):  # a busy user: 30 screener runs and overview loads a minute
            r = c.post("/api/screener/run", json={"filters": [{"field": "rsi14", "min": 0}], "limit": 5}).json()
            c.get("/api/markets/overview")
            c.get("/api/screener/fields")
            wall.t += 2
        snaps, hist = calls(tr, "/v2/stocks/snapshots", before), calls(tr, "/v2/stocks/bars", before)
        assert snaps <= dd.SCAN_PER_MINUTE, f"minute {minute}: {snaps} snapshot calls"
        assert hist <= dd.HISTORY_PER_MINUTE, f"minute {minute}: {hist} bar calls"
        seen.append(r["universe"]["with_quotes"])
    # the scan makes steady progress through the universe and says so
    assert seen == sorted(seen) and seen[-1] == 5 * dd.SCAN_PER_MINUTE * dd.SCAN_BATCH
    u = r["universe"]
    assert u["remote"] is True and u["total"] == 11_000 and not u["complete"]
    assert u["fetched_from"] <= u["fetched_to"] <= wall.t and "20 requests a minute" in u["note"]
    assert r["history_pending"] > 0 and u["with_history"] <= 5 * dd.HISTORY_PER_MINUTE
    # every symbol was asked for at most once per scan pass, 100 at a time
    asked = [s for q in tr.requests[start:] if q["path"] == "/v2/stocks/snapshots" for s in q["params"]["symbols"].split(",")]
    assert len(asked) == len(set(asked)) and max(len(q["params"]["symbols"].split(",")) for q in tr.requests[start:]
                                                  if q["path"] == "/v2/stocks/snapshots") <= dd.SCAN_BATCH
    # scanned symbols are not added to the live stream or the poller's interest
    assert len(feed.wanted()) < 10 and feed.protocol.cap == 30
    # fields Alpaca does not provide are marked unavailable rather than guessed
    fields = {f["key"]: f for f in c.get("/api/screener/fields").json()["fields"]}
    assert not fields["market_cap"]["available"] and not fields["sector"]["available"] and fields["rsi14"]["available"]
    o = c.get("/api/markets/overview").json()
    assert o["sizing"] == "dollar_volume" and {s["sector"] for s in o["sectors"]} <= set(dd.SECTOR_ETFS.values())
    assert {t["symbol"] for t in o["indices"]} == {"SPY", "QQQ"}


def test_live_watchlist_history_is_budgeted():
    c, ex, feed, tr, wall = live_app(n=500)
    names = [s for s in sorted(feed.symbols()) if s not in ("SPY", "QQQ", "XLK", "XLF")][:100]
    c.patch("/api/watchlist-sets/Main", json={"symbols": names})
    before = len(tr.requests)
    for _ in range(10):
        j = c.get("/api/watchlist-sets/Main/rows", params={"columns": "last,range_52w,sparkline"}).json()
    assert calls(tr, "/v2/stocks/bars", before) <= dd.HISTORY_PER_MINUTE
    assert j["note"] and "at most 20 symbols a minute" in j["note"]
    have = [r for r in j["rows"] if r.get("sparkline") or r.get("history")]
    assert 0 < len(have) <= dd.HISTORY_PER_MINUTE
    wall.t += 61
    c.get("/api/watchlist-sets/Main/rows", params={"columns": "last,range_52w,sparkline"})
    assert calls(tr, "/v2/stocks/bars", before) <= 2 * dd.HISTORY_PER_MINUTE


def test_visible_watchlist_is_streamed_first_and_hidden_lists_are_not_polled():
    c, ex, feed, tr, wall = live_app(n=300)
    pool = [s for s in sorted(feed.symbols()) if len(s) == 4]
    a, b, cc = pool[:40], pool[40:80], pool[80:120]
    c.patch("/api/watchlist-sets/Main", json={"symbols": a})
    c.post("/api/watchlist-sets", json={"name": "B", "symbols": b})
    c.post("/api/watchlist-sets", json={"name": "C", "symbols": cc})
    # Before any list is shown every list is of interest (the earlier behaviour).
    assert set(a + b + cc) <= set(feed.wanted())
    aid = list(ex.accounts)[0]
    feed.apply_snapshots({"QQQ": fixture("alpaca/stock_snapshots.json")["AAPL"]})
    ex.tick()
    ex.submit(aid, {"symbol": "QQQ", "side": "buy", "qty": 1})
    c.get("/api/watchlist-sets/B/rows")  # the browser shows list B
    w = feed.wanted()
    assert w[0] == "QQQ" and w[1:41] == b  # positions first, then the visible list
    assert not set(a + cc) & set(w)
    feed.poll_once()
    assert feed.stream._wanted[:30] == w[:30] and set(feed.stream._wanted) == set(w)
    assert len(feed.protocol.target()) <= 30
    # Renaming keeps the declaration; after two minutes without a request the declaration lapses.
    c.patch("/api/watchlist-sets/B", json={"name": "Bee"})
    assert feed.wanted()[1:41] == b
    ex.clock.advance(121)
    assert set(a + b + cc) <= set(feed.wanted())
