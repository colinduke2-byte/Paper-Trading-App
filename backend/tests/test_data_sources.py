"""Feed selection, .env loading, research data and the API surface of a live feed. No network."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata import config
from app.marketdata.alpaca import AlpacaFeed
from app.marketdata.finnhub import FinnhubClient, earnings_records, fundamentals_record, news_records as fh_news
from app.marketdata.research import ResearchHub, corporate_action_records, news_records
from app.marketdata.synthetic import SyntheticFeed

from test_alpaca import FakeTransport, Wall, contracts_for, fixture, raw, routes, ts

D = Decimal
NOW = 1795500000.0


# ---- .env and feed selection ----------------------------------------------------------------------
def test_load_dotenv_handles_quotes_comments_and_paths_with_spaces(tmp_path):
    d = tmp_path / "my folder" / "Paper Desk"
    d.mkdir(parents=True)
    env_file = d / ".env"
    env_file.write_text("\ufeff# keys\nexport APCA_API_KEY_ID=PKABC\nAPCA_API_SECRET_KEY=\"s3cr=et\"\n"
                        "PAPERDESK_DATA='C:\\Users\\Me\\Paper Desk\\data'\nFINNHUB_API_KEY=abc # note\n"
                        "ALREADY=new\nbroken line\n", encoding="utf-8")
    env = {"ALREADY": "old"}
    names = config.load_dotenv(env_file, env)
    assert env["APCA_API_KEY_ID"] == "PKABC" and env["APCA_API_SECRET_KEY"] == "s3cr=et"
    assert env["PAPERDESK_DATA"] == "C:\\Users\\Me\\Paper Desk\\data" and env["FINNHUB_API_KEY"] == "abc"
    assert env["ALREADY"] == "old" and "ALREADY" not in names
    assert config.load_dotenv(tmp_path / "missing.env", env) == []


def test_no_key_falls_back_to_synthetic_and_says_why(tmp_path):
    sel = config.select_feed(NOW, str(tmp_path), environ={}, dotenv=tmp_path / "none")
    assert isinstance(sel.feed, SyntheticFeed) and sel.session_mode == "always" and sel.data_dir == str(tmp_path)
    assert "APCA_API_KEY_ID" in sel.reason and sel.feed.status_info()["fallback_reason"] == sel.reason
    sel = config.select_feed(NOW, str(tmp_path), environ={"PAPERDESK_FEED": "alpaca"}, dotenv=tmp_path / "none")
    assert isinstance(sel.feed, SyntheticFeed) and "APCA_API_SECRET_KEY" in sel.reason
    sel = config.select_feed(NOW, str(tmp_path), environ={"PAPERDESK_FEED": "synthetic", "APCA_API_KEY_ID": "k",
                                                          "APCA_API_SECRET_KEY": "s"}, dotenv=tmp_path / "none")
    assert isinstance(sel.feed, SyntheticFeed) and "synthetic" in sel.reason
    sel = config.select_feed(NOW, str(tmp_path), environ={"PAPERDESK_FEED": "bloomberg"}, dotenv=tmp_path / "none")
    assert isinstance(sel.feed, SyntheticFeed) and "not recognised" in sel.reason


def test_rejected_key_falls_back_with_the_cause(tmp_path):
    env = {"APCA_API_KEY_ID": "PKBAD", "APCA_API_SECRET_KEY": "bad"}
    tr = FakeTransport({"/v2/assets": (403, {"message": "forbidden"})})
    sel = config.select_feed(NOW, str(tmp_path), environ=env, dotenv=tmp_path / "none", start=False,
                             transport=tr, sleep=lambda s: None)
    assert isinstance(sel.feed, SyntheticFeed) and "rejected the API key (HTTP 403)" in sel.reason
    assert "PKBAD" not in sel.reason and "bad" not in sel.reason.replace("rejected", "")


def test_keys_select_alpaca_with_real_market_hours(tmp_path):
    (tmp_path / ".env").write_text("APCA_API_KEY_ID=PKTEST\nAPCA_API_SECRET_KEY=secret\nFINNHUB_API_KEY=fh\n",
                                   encoding="utf-8")
    env = {}
    tr = FakeTransport(routes())
    sel = config.select_feed(NOW, str(tmp_path), environ=env, dotenv=tmp_path / ".env", start=False, transport=tr,
                             sleep=lambda s: None)
    assert isinstance(sel.feed, AlpacaFeed) and sel.session_mode == "us" and sel.reason is None
    assert Path(sel.data_dir) == tmp_path / "alpaca" and "SPY" in sel.watchlist
    assert sel.feed.research.finnhub is not None and sel.feed.research.alpaca is sel.feed.rest
    assert tr.requests[0]["host"] == "paper-api.alpaca.markets"
    env2 = {"APCA_API_KEY_ID": "k", "APCA_API_SECRET_KEY": "s", "PAPERDESK_SESSIONS": "always",
            "APCA_API_BASE_URL": "https://api.alpaca.markets/", "PAPERDESK_ALPACA_FEED": "sip"}
    tr2 = FakeTransport(routes())
    sel = config.select_feed(NOW, str(tmp_path / "b"), environ=env2, dotenv=tmp_path / "none", start=False,
                             transport=tr2, sleep=lambda s: None)
    assert sel.session_mode == "always" and sel.feed.data_feed == "sip" and tr2.requests[0]["host"] == "api.alpaca.markets"


def test_unreachable_provider_falls_back(tmp_path):
    tr = FakeTransport({})
    tr.get = lambda *a: (_ for _ in ()).throw(OSError("timed out"))
    sel = config.select_feed(NOW, str(tmp_path), environ={"APCA_API_KEY_ID": "k", "APCA_API_SECRET_KEY": "s"},
                             dotenv=tmp_path / "none", start=False, transport=tr, sleep=lambda s: None)
    assert isinstance(sel.feed, SyntheticFeed) and "Could not reach Alpaca" in sel.reason


def test_manual_check_without_keys_prints_fail_and_no_secrets(monkeypatch, capsys, tmp_path):
    from app.marketdata import alpaca
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.setenv("PAPERDESK_ENV_FILE", str(tmp_path / "none"))
    assert alpaca._check() == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL keys:") and out.splitlines()[-1] == "SUMMARY: 0 of 1 checks passed"


# ---- research parsing ------------------------------------------------------------------------------
def test_corporate_actions_are_neutral_decimal_records():
    recs = corporate_action_records(fixture("alpaca/corporate_actions.json")["corporate_actions"])
    assert [r["type"] for r in recs] == ["reverse_split", "forward_split", "cash_dividend"]
    div = recs[-1]
    assert div["symbol"] == "AAPL" and div["rate"] == D("0.26") and isinstance(div["rate"], Decimal)
    assert (div["ex_date"], div["record_date"], div["payable_date"]) == ("2026-11-10", "2026-11-10", "2026-11-13")
    split = recs[1]
    assert (split["old_rate"], split["new_rate"]) == (D(1), D(10)) and split["effective_date"] is None
    assert all(r["source"] == "alpaca" for r in recs)


def test_news_records_from_both_providers():
    a = news_records(fixture("alpaca/news.json")["news"])
    assert a[0]["headline"].startswith("Analysts Adjust") and a[0]["created_at"] == ts("2026-10-21T15:02:10Z")
    assert a[1]["image"].endswith("width=250&height=187") and a[0]["provider"] == "alpaca"
    f = fh_news(fixture("finnhub/company_news.json"))
    assert f[0]["created_at"] >= f[-1]["created_at"] and f[0]["provider"] == "finnhub"


def test_finnhub_earnings_and_fundamentals():
    e = earnings_records(fixture("finnhub/earnings_calendar.json"))
    assert [r["date"] for r in e] == ["2019-10-30", "2020-01-28"]
    assert e[1]["eps_actual"] == D("4.99") and e[1]["hour"] == "after close" and e[1]["revenue_actual"] == D(91819000000)
    f = fundamentals_record(fixture("finnhub/company_profile2.json"), fixture("finnhub/basic_financials.json"))
    assert f["name"] == "Apple Inc" and f["market_cap"] == D(1415993) * 1_000_000
    assert f["high_52w"] == D("310.43") and f["beta"] == D("1.2989")


def test_finnhub_uses_the_token_header():
    tr = FakeTransport({"/api/v1/calendar/earnings": raw("finnhub/earnings_calendar.json")})
    fh = FinnhubClient("fh-token", transport=tr, sleep=lambda s: None)
    fh.earnings("AAPL", date(2020, 1, 1), date(2020, 2, 1))
    r = tr.requests[0]
    assert r["host"] == "finnhub.io" and r["headers"]["X-Finnhub-Token"] == "fh-token" and "fh-token" not in r["url"]
    assert r["params"] == {"from": "2020-01-01", "to": "2020-02-01", "symbol": "AAPL"}


def test_research_hub_serves_saved_data_when_refresh_fails():
    wall = Wall(1000.0)
    tr = FakeTransport({"/v1beta1/news": [raw("alpaca/news.json"), (500, "down")]})
    from app.marketdata.alpaca import AlpacaREST
    hub = ResearchHub(alpaca=AlpacaREST("k", "s", tr, sleep=lambda s: None), clock=wall)
    first = hub.news("TSLA")
    assert first["available"] and not first["stale"] and first["source"] == "Alpaca" and first["as_of"] == 1000.0
    wall.t += 301
    again = hub.news("TSLA")
    assert again["stale"] and again["available"] and "refresh failed" in again["reason"] and again["items"]
    none = ResearchHub().earnings("AAPL")
    assert not none["available"] and "FINNHUB_API_KEY" in none["reason"]


# ---- API with a live feed ------------------------------------------------------------------------------
def live_client(t="2024-04-22T15:00:00Z", **route_overrides):
    wall = Wall(ts(t))
    tr = FakeTransport(routes(**{"/v2/options/contracts": contracts_for("2024-04-26"),
                                 "/api/v1/calendar/earnings": raw("finnhub/earnings_calendar.json"),
                                 "/api/v1/stock/profile2": raw("finnhub/company_profile2.json"),
                                 "/api/v1/stock/metric": raw("finnhub/basic_financials.json"), **route_overrides}))
    feed = AlpacaFeed("k", "s", transport=tr, sleep=lambda s: None, wall_clock=wall)
    feed.load_assets()
    feed.research = ResearchHub(alpaca=feed.rest, finnhub=FinnhubClient("f", transport=tr, sleep=lambda s: None),
                                clock=wall)
    ex = Exchange(feed, SimClock(ts(t)), session_mode="us")
    ex.create_account("Margin", "margin")
    feed.apply_snapshots(json.loads(raw("alpaca/stock_snapshots.json"), parse_float=D))
    ex.tick()
    return TestClient(create_app(ex, run_loop=False)), ex, feed, tr


def test_status_names_the_live_source():
    c, ex, feed, _ = live_client()
    st = c.get("/api/status").json()
    assert st["feed"] == "alpaca" and st["simulated_data"] is False and st["realtime"] is True
    assert st["data_exchange"] == "IEX" and st["session_mode"] == "us" and st["stream"]["cap"] == 30
    assert st["options_source"].startswith("Alpaca indicative")


def test_option_routes_use_provider_data():
    c, ex, feed, tr = live_client()
    exps = c.get("/api/options/expirations/AAPL").json()
    assert exps == [{"date": "2024-04-26", "dte": 4}]
    ch = c.get("/api/options/chain/AAPL", params={"expiry": "2024-04-26"}).json()
    assert ch["simulated"] is False and ch["source"] == "alpaca-indicative" and ch["rows"][0]["call"]["ask"] == 4.3
    assert ch["rows"][0]["put"]["source"] == "model"
    assert c.get("/api/options/chain/AAPL", params={"expiry": "2024-05-03"}).status_code == 404
    q = c.get("/api/quotes", params={"symbols": "AAPL240426C00162500"}).json()
    assert q["AAPL240426C00162500"]["bid"] == 4.15


def test_option_routes_fall_back_to_the_model_when_the_provider_has_nothing():
    c, ex, feed, tr = live_client(**{"/v2/options/contracts": json.dumps({"option_contracts": []}),
                                     "/v1beta1/options/snapshots/AAPL": json.dumps({"snapshots": {}})})
    exps = c.get("/api/options/expirations/AAPL").json()
    assert len(exps) >= 5  # the model calendar
    ch = c.get("/api/options/chain/AAPL", params={"expiry": exps[0]["date"]}).json()
    assert ch["simulated"] is True and ch["source"] == "model" and ch["rows"]


def test_research_routes_live():
    c, ex, feed, tr = live_client(t="2020-01-10T15:00:00Z")
    news = c.get("/api/research/news/AAPL").json()
    assert news["source"] == "Alpaca" and news["available"] and news["items"][0]["headline"]
    cal = c.get("/api/research/calendar/AAPL").json()
    assert cal["earnings"]["source"] == "Finnhub" and cal["earnings"]["items"][0]["eps_actual"] == 3.03
    assert cal["dividends"]["items"][0]["rate"] == 0.26 and cal["splits"]["items"]
    ca_req = [r for r in tr.requests if r["path"] == "/v1/corporate-actions"][0]["params"]
    assert ca_req["symbols"] == "AAPL" and ca_req["types"] == "cash_dividend,forward_split,reverse_split"
    f = c.get("/api/research/fundamentals/AAPL").json()
    assert f["data"]["name"] == "Apple Inc" and f["source"] == "Finnhub"
    assert c.get("/api/research/news/ZZZZ").status_code == 404
    assert c.get("/api/research/corporate-actions/AAPL", params={"start": "soon"}).status_code == 400
    assert c.get("/api/research/sources").json()["earnings"] == "Finnhub"


def test_research_routes_on_the_synthetic_feed_say_unavailable():
    ex = Exchange(SyntheticFeed(seed=3, now=NOW))
    c = TestClient(create_app(ex, run_loop=False))
    for path in ("news", "fundamentals", "corporate-actions"):
        r = c.get(f"/api/research/{path}/NVLX").json()
        assert r["available"] is False and "fictional" in r["reason"]
    cal = c.get("/api/research/calendar/NVLX").json()
    assert cal["earnings"]["available"] is False
    st = c.get("/api/status").json()
    assert st["realtime"] is False and st["data_source"] == "simulated"


def test_live_feed_follows_browser_subscriptions():
    c, ex, feed, tr = live_client()
    ex.watchlists = {"Main": ["SPY"]}
    c.app.state.ctx.clients[object()] = {"symbols": ["TSLA"], "account": None}
    assert feed.wanted()[:2] == ["SPY", "TSLA"]
