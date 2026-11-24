"""Simulated research: statements, earnings (price gaps, volatility), news, economic calendar, routes."""
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

from app.api.server import create_app, plain
from app.core import calendar as cal
from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata.feed import SimFeed
from app.marketdata.replay.control import ReplayController
from app.marketdata.replay.format import SUFFIX
from app.marketdata.replay.library import BUNDLED_DIR
from app.marketdata.replay.samples import SAMPLES
from app.marketdata.simresearch import SimResearch, macro
from app.marketdata.simresearch.profiles import ANCHOR_QUARTER, quarter_end
from app.marketdata.synthetic import UNIVERSE, SyntheticFeed
from app.options import chain as optchain
from app.options.symbology import Contract, build

D = Decimal
NOW = 1795500000.0  # 2026-11-23 11:20 UTC
STOCKS = [u[0] for u in UNIVERSE if u[2] != "ETF"]


def feed(seed=7, now=NOW, **kw) -> SyntheticFeed:
    return SyntheticFeed(seed=seed, now=now, history_days=300, **kw)


def frozen(f: SyntheticFeed) -> SyntheticFeed:
    """No random walk: prices then move only by releases (and corporate actions)."""
    f.rng.gauss = lambda mu=0.0, sigma=1.0: 0.0
    return f


def content(h: SimResearch, now: float, symbols=("NVLX", "QNTM", "UTIL", "TECX", "MKTX")) -> str:
    out = {"market": h.market_news(50, now=now), "econ": h.economic_calendar(date(2026, 9, 1), date(2027, 3, 1), now),
           "earn": h.earnings_calendar(date(2026, 10, 1), date(2027, 3, 1), now)}
    for s in symbols:
        out[s] = [h.fundamentals(s, now), h.statements(s, now), h.calendar(s, now), h.news(s, 50, now)]
    return json.dumps(plain(out), sort_keys=True)


# ---- determinism ---------------------------------------------------------------------------------------
def test_same_seed_and_time_give_identical_content():
    assert content(feed().research, NOW) == content(feed().research, NOW)
    assert content(feed().research, NOW) != content(feed(seed=8).research, NOW)


def test_content_survives_a_restart(tmp_path):
    a = feed()
    snap = json.loads(json.dumps(a.snapshot()))
    later = feed(now=NOW + 7200)  # a new process, started later, restores the saved market
    later.restore(snap)
    assert later.sim_born == a.sim_born and later._moves_through == a._moves_through
    assert content(later.research, NOW + 3600) == content(a.research, NOW + 3600)


def sample(i):
    return os.path.join(BUNDLED_DIR, SAMPLES[i][0] + SUFFIX)


def test_replay_of_a_synthetic_session_has_research_and_seeks_are_deterministic():
    a, b = ReplayController(sample(0)), ReplayController(sample(0))
    h = a.feed.research
    assert isinstance(h, SimResearch) and h.seed == SAMPLES[0][2] and not h.applies_moves
    target = a.start + 4 * 3600 + 17
    a.seek(target)
    while b.now() < target:
        b.step(min(900, target - b.now()))
    assert a.now() == b.now() == target
    syms = tuple(sorted(s for s in a.feed.info if s in STOCKS))[:3] + ("MKTX",)
    assert content(a.feed.research, a.now(), syms) == content(b.feed.research, b.now(), syms)
    # option volatility in the replay includes the same event term
    for s in syms[:3]:
        exp = a.ex.now() + 120 * 86400
        assert a.feed.event_variance(s, exp, a.now()) == b.feed.event_variance(s, exp, b.now()) > 0


def test_replay_of_unknown_symbols_has_no_simulated_research(tmp_path):
    from tests.test_replay import write_tiny  # a tiny session of the fictional-but-unlisted symbol ABC
    path = write_tiny(tmp_path / ("tiny" + SUFFIX))
    c = ReplayController(str(path))
    assert c.feed.research is None and c.feed.event_variance("ABC", c.now() + 86400 * 30, c.now()) == 0.0


# ---- statements ----------------------------------------------------------------------------------------
_STATIC = [feed()]  # one feed shared by the read-only statement tests


@settings(max_examples=60, deadline=None)
@given(seed=st.integers(0, 10_000), sym=st.sampled_from(STOCKS), k=st.integers(2015 * 4, 2028 * 4))
def test_statement_identities(seed, sym, k):
    h = SimResearch(_STATIC[0], seed)
    m, sh = h.model(sym), h.shares(sym, None)
    q, prev = m.quarter(k, sh), m.quarter(k - 1, sh)
    inc, bs, cf = q["income"], q["balance"], q["cash_flow"]
    assert bs["total_assets"] == bs["total_liabilities"] + bs["equity"]
    assert bs["total_assets"] == sum(bs[x] for x in ("cash", "receivables", "inventory", "property_equipment", "other_assets"))
    assert bs["total_liabilities"] == bs["debt"] + bs["accounts_payable"] + bs["other_liabilities"]
    assert min(bs["cash"], bs["receivables"], bs["property_equipment"], bs["accounts_payable"], bs["debt"]) >= 0
    assert cf["operating"] + cf["investing"] + cf["financing"] == cf["net_change_in_cash"] == cf["cash_end"] - cf["cash_start"]
    assert cf["cash_start"] == prev["balance"]["cash"] and cf["cash_end"] == bs["cash"]
    assert cf["operating"] == cf["net_income"] + cf["depreciation"] + cf["working_capital"]
    assert inc["gross_profit"] == inc["revenue"] - inc["cost_of_revenue"]
    assert inc["operating_income"] == inc["gross_profit"] - inc["operating_expenses"]
    assert inc["net_income"] == inc["pretax_income"] - inc["income_tax"] and inc["income_tax"] >= 0
    assert abs(inc["eps"] * inc["shares"] - inc["net_income"]) <= inc["shares"] * D("0.005")
    assert inc["revenue"] > 0 and inc["cost_of_revenue"] >= 0
    y = m.year(k // 4, sh)
    quarters = [m.quarter(4 * (k // 4) + i, sh) for i in range(4)]
    assert y["income"]["revenue"] == sum(x["income"]["revenue"] for x in quarters)
    assert y["cash_flow"]["cash_end"] - y["cash_flow"]["cash_start"] == y["cash_flow"]["net_change_in_cash"]
    assert y["balance"] == quarters[-1]["balance"]


@pytest.mark.parametrize("sym", STOCKS)
def test_listed_eps_is_the_sum_of_the_anchor_quarters(sym):
    h = _STATIC[0].research
    m, sh = h.model(sym), h.shares(sym, None)
    ttm = sum(m.quarter(k, sh)["income"]["net_income"] for k in range(ANCHOR_QUARTER - 3, ANCHOR_QUARTER + 1))
    listed = D(repr(_STATIC[0].info[sym]["eps"])) * sh
    assert abs(ttm - listed) <= 4  # whole-dollar rounding of four quarters
    sign = 1 if listed > 0 else -1
    assert all(sign * m.quarter(k, sh)["income"]["net_income"] > 0 for k in range(ANCHOR_QUARTER - 12, ANCHOR_QUARTER + 4))


def test_fundamentals_ratios_follow_from_the_statements():
    f = _STATIC[0]
    h = f.research
    d = h.fundamentals("UTIL", NOW)["data"]
    st_ = h.statements("UTIL", NOW)["data"]
    last4 = st_["quarterly"][:4]
    ni = sum(q["income"]["net_income"] for q in last4)
    rev = sum(q["income"]["revenue"] for q in last4)
    sh = last4[0]["income"]["shares"]
    px = f.quote("UTIL").last
    assert d["eps"] == (ni / sh).quantize(D("0.01")) and d["market_cap"] == px * sh
    assert d["pe"] == D(repr(float(px / d["eps"]))).quantize(D("0.01"))
    assert abs(d["ps"] - px * sh / rev) < D("0.01")
    divs = -sum(q["cash_flow"]["dividends_paid"] for q in last4)
    assert abs(d["payout_ratio_pct"] - divs / ni * 100) < D("0.01") and 30 < d["payout_ratio_pct"] < 100
    bs = last4[0]["balance"]
    assert abs(d["debt_to_equity"] - bs["debt"] / bs["equity"]) < D("0.01")
    assert d["source"] == "simulated" and d["next_earnings"]["date"] > "2026-11-23"
    assert st_["quarterly"][0]["period"] == d["last_period"] == "Q3 2026"
    assert [a["period"] for a in st_["annual"]] == ["FY 2025", "FY 2024", "FY 2023"]


def test_fund_holdings_and_sector_weights():
    h = _STATIC[0].research
    tecx, mktx = h.fundamentals("TECX", NOW)["data"], h.fundamentals("MKTX", NOW)["data"]
    assert {r["sector"] for r in tecx["sector_weights"]} == {"Technology", "Communication"}
    assert mktx["holdings_count"] == len(STOCKS)
    for f in (tecx, mktx):
        assert abs(sum(r["weight_pct"] for r in f["holdings"]) - 100) < D("0.1")
        assert f["kind"] == "fund" and f["pe"] > 0 and f["eps"] is None
    assert h.statements("TECX", NOW)["data"]["quarterly"] == [] and h.earnings("TECX", NOW)["items"] == []


def test_a_split_keeps_per_share_figures_consistent():
    from app.lifecycle.actions import split
    f = feed()
    h = f.research
    before = h.fundamentals("NVLX", NOW)["data"]
    act = split("NVLX", cal.ny_date(NOW) - timedelta(days=1), 2, 1)
    f.corporate_actions = lambda s, e, _orig=f.corporate_actions: _orig(s, e) + ([act] if s <= act.ex_date <= e else [])
    f.apply_corporate_action(act)
    after = h.fundamentals("NVLX", NOW)["data"]
    assert after["shares_outstanding"] == 2 * before["shares_outstanding"]
    assert abs(after["eps"] * 2 - before["eps"]) <= D("0.01") and after["revenue_ttm"] == before["revenue_ttm"]
    assert abs(after["market_cap"] - before["market_cap"]) <= after["shares_outstanding"] * D("0.01")
    assert after["payout_ratio_pct"] == before["payout_ratio_pct"]


# ---- earnings: calendar, price gap, volatility ---------------------------------------------------------
def first_release(h, sym, after=NOW):
    return h.upcoming_releases(sym, after, after + 200 * 86400)[0]


def test_reference_earnings_case():
    """Seed 7, NVLX, Q4 2026: the numbers are fixed by the seed (update only on purpose)."""
    h = feed().research
    ts, k = first_release(h, "NVLX")
    assert (k, datetime.fromtimestamp(ts, cal.NY).isoformat()) == (2026 * 4 + 3, "2027-02-12T08:00:00-05:00")
    up = h.earnings_record("NVLX", k, ts - 1)
    assert up["status"] == "upcoming" and up["eps_actual"] is None and up["eps_estimate"] == D("1.13")
    done = h.earnings_record("NVLX", k, ts)
    assert (done["eps_actual"], done["result"], done["guidance"]) == (D("1.11"), "miss", "reaffirmed")
    assert done["price_reaction_pct"] == D("-4.05")  # exp(gap) - 1, gap about -0.0414
    assert round(h.gap("NVLX", k), 4) == -0.0414


def test_earnings_gap_moves_the_price_once_and_survives_restart(tmp_path):
    f = frozen(feed())
    h = f.research
    ts, k = first_release(h, "ORBT")  # Q4 2026 beat, after the close on 2027-02-01
    f.step(ts - 2)
    before = f.quote("ORBT").last
    f.step(ts - 1)
    assert f.quote("ORBT").last == before  # nothing due yet
    snap = json.loads(json.dumps(f.snapshot()))
    f.step(ts)
    after = f.quote("ORBT").last
    expected = (before * D(repr(math.exp(h.gap("ORBT", k))))).quantize(D("0.01"))
    assert after == expected and after > before
    f.step(ts + 1)
    assert f.quote("ORBT").last == after  # applied once
    # a restart from the save taken before the release applies it on the first step after
    g = frozen(feed(now=ts + 500))
    g.restore(snap)
    g.step(ts + 600)
    assert g.quote("ORBT").last == expected
    g.step(ts + 601)
    assert g.quote("ORBT").last == expected
    news = h.news("ORBT", 50, ts + 10)["items"]
    item = next(i for i in news if i["id"] == f"sim-earnings-ORBT-{k}")
    assert "gapped up 6.0%" in item["summary"] and item["created_at"] == ts


def test_releases_from_before_the_feed_started_move_nothing():
    h = feed().research
    last = h.last_reported("NVLX", NOW)
    assert h.earnings_record("NVLX", last, NOW)["price_reaction_pct"] is None
    f = frozen(feed())
    p = f.quote("NVLX").last
    f.step(NOW + 1)
    assert f.quote("NVLX").last == p


def _exchange(f, mode="always", start=None):
    clock = SimClock(start or f.sim_born)
    ex = Exchange(f, clock, session_mode=mode)
    ex.tick()
    return ex, clock


@pytest.mark.parametrize("sym", ["TELE", "BLDR"])  # both gap down before the open (seed 7)
def test_a_resting_stop_fills_at_the_gap_price(sym):
    f = frozen(feed())
    h = f.research
    ts, k = first_release(h, sym)
    gap = h.gap(sym, k)
    assert gap < -0.04
    ex, clock = _exchange(f)
    clock.set(ts - 3600)
    ex.tick()
    a = ex.create_account("t", "margin", D(100000)).id
    ex.submit(a, {"symbol": sym, "side": "buy", "qty": 100})
    ref = f.quote(sym).last
    stop = (ref * D("0.985")).quantize(D("0.01"))
    [o] = ex.submit(a, {"symbol": sym, "side": "sell", "qty": 100, "type": "stop", "stop_price": str(stop), "tif": "gtc"})
    clock.set(ts - 1)
    ex.tick()
    assert ex.engine.orders[o.id].status == "working"
    clock.set(ts)
    ex.tick()
    o = ex.engine.orders[o.id]
    q = f.quote(sym)
    assert o.status == "filled" and o.avg_fill_price == q.bid  # the gapped market, not the stop price
    assert o.avg_fill_price < stop and abs(float(q.last / ref) - math.exp(gap)) < 0.001


def test_after_close_gap_in_us_hours_fills_the_stop_at_the_next_open():
    f = frozen(feed(always_open=False))
    h = f.research
    ts, k = first_release(h, "SOLR")  # after the close on 2027-02-04, gaps down
    assert h.profile("SOLR").hour == "after close" and h.gap("SOLR", k) < -0.03
    ex, clock = _exchange(f, "us")
    day = cal.ny_date(ts)
    clock.set(datetime.combine(day, datetime.min.time().replace(hour=15), tzinfo=cal.NY).timestamp())
    ex.tick()
    a = ex.create_account("t", "margin", D(100000)).id
    ex.submit(a, {"symbol": "SOLR", "side": "buy", "qty": 100})
    stop = (f.quote("SOLR").last * D("0.99")).quantize(D("0.01"))
    [o] = ex.submit(a, {"symbol": "SOLR", "side": "sell", "qty": 100, "type": "stop", "stop_price": str(stop), "tif": "gtc"})
    clock.set(ts + 60)  # post-market: the price has gapped, but stops only work in the regular session
    ex.tick()
    assert f.quote("SOLR").last < stop and ex.engine.orders[o.id].status == "working"
    nxt = day + timedelta(days=1)
    while not cal.is_trading_day(nxt):
        nxt += timedelta(days=1)
    clock.set(datetime.combine(nxt, cal.OPEN, tzinfo=cal.NY).timestamp() + 1)
    ex.tick()
    o = ex.engine.orders[o.id]
    assert o.status == "filled" and o.avg_fill_price < stop


def _iv(f, sym, expiry: date, now: float) -> float:
    S = float(f.quote(sym).last)
    k = optchain.strikes(S)[len(optchain.strikes(S)) // 2]
    c = Contract(build(sym, expiry, "C", k), sym, expiry, "C", k)
    return optchain.option_snapshot(c, S, f.base_vol(sym), now, f.event_variance(sym, c.expiry_ts, now))["iv"]


def test_implied_volatility_rises_into_earnings_and_drops_after():
    f = feed()
    h = f.research
    ts, k = first_release(h, "AUTO")  # before the open on 2027-02-16 (a Tuesday)
    rel_day = cal.ny_date(ts)
    expiry = rel_day + timedelta(days=(4 - rel_day.weekday()) % 7)  # that week's Friday, after the report
    m = h.profile("AUTO").expected_move
    week_before, day_before, after = ts - 7 * 86400, ts - 3600 * 12, ts + 60
    iv_week, iv_day, iv_after = _iv(f, "AUTO", expiry, week_before), _iv(f, "AUTO", expiry, day_before), _iv(f, "AUTO", expiry, after)
    assert iv_day > iv_week > iv_after
    # the event term adds exactly the gap variance on top of the base surface
    T = (cal.close_ts(expiry) - day_before) / optchain.YEAR_SECONDS
    S = float(f.quote("AUTO").last)
    strike = optchain.strikes(S)[len(optchain.strikes(S)) // 2]
    base = optchain.surface_vol(f.base_vol("AUTO"), S, float(strike), T)
    assert abs(iv_day ** 2 - base ** 2 - m * m / T) < 1e-9 or iv_day == 3.0
    # an expiry before the report has no event term
    early = rel_day - timedelta(days=(rel_day.weekday() - 4) % 7 or 7)
    assert f.event_variance("AUTO", cal.close_ts(early), day_before) == 0.0
    assert f.event_variance("MKTX", cal.close_ts(expiry), day_before) == 0.0  # funds do not report


def test_chain_route_uses_the_event_term():
    f = feed()
    ts, _ = first_release(f.research, "AUTO")
    ex, clock = _exchange(f, start=ts - 86400 * 6)
    c = TestClient(create_app(ex, run_loop=False))
    exps = c.get("/api/options/expirations/AUTO").json()
    rel = cal.ny_date(ts).isoformat()
    after = next(e["date"] for e in exps if e["date"] >= rel)
    before = [e["date"] for e in exps if e["date"] < rel][0]
    mid = lambda rows: rows[len(rows) // 2]["call"]["iv"]
    assert mid(c.get(f"/api/options/chain/AUTO?expiry={after}").json()["rows"]) > \
        1.5 * mid(c.get(f"/api/options/chain/AUTO?expiry={before}").json()["rows"])


# ---- economic calendar ------------------------------------------------------------------------------------
def test_economic_schedule_rules():
    evs = macro.releases(7, macro.ny_ts(date(2026, 1, 1), macro.time(0)), macro.ny_ts(date(2026, 12, 31), macro.time(23)))
    kinds = {}
    for e in evs:
        kinds.setdefault(e["kind"], []).append(date.fromisoformat(e["date"]))
        assert cal.is_trading_day(date.fromisoformat(e["date"]))
    assert len(kinds["jobs"]) == len(kinds["cpi"]) == len(kinds["retail"]) == 12
    assert len(kinds["rate"]) == 8 and len(kinds["gdp"]) == 4
    assert all(d.day <= 7 or not cal.is_trading_day(d - timedelta(days=7)) for d in kinds["jobs"])
    assert all(d.weekday() == 2 and (d + timedelta(days=7)).month != d.month for d in kinds["rate"])
    assert date(2026, 6, 17) not in kinds["rate"]  # the bundled replay sample day stays release-free in session


def test_economic_release_moves_the_market_by_beta():
    f = frozen(feed())
    h = f.research
    e = macro.releases(7, NOW, NOW + 40 * 86400)[0]
    f.step(e["ts"] - 1)
    before = {s: f.quote(s).last for s in f.info}
    f.step(e["ts"])
    for s in ("NVLX", "UTIL", "MKTX"):
        want = (before[s] * D(repr(math.exp(h.beta(s) * e["market_move"])))).quantize(D("0.01"))
        assert f.quote(s).last == want
    row = h.economic_calendar(date.fromisoformat(e["date"]), date.fromisoformat(e["date"]), e["ts"] - 1)["items"][0]
    assert row["actual"] is None and row["market_move_pct"] is None
    row = h.economic_calendar(date.fromisoformat(e["date"]), date.fromisoformat(e["date"]), e["ts"])["items"][0]
    assert row["actual"] is not None and row["market_move_pct"] == D(repr((math.exp(e["market_move"]) - 1) * 100)).quantize(D("0.01"))


# ---- news -------------------------------------------------------------------------------------------------
REAL_WORDS = ("Federal Reserve", "Fed ", "Nasdaq", "NYSE", "S&P", "Dow ", "Bureau of Labor", "Goldman", "Morgan")


@pytest.mark.parametrize("now", [NOW, NOW + 86400 * 75, NOW + 86400 * 90.3])
def test_news_only_references_events_that_happened(now):
    f = frozen(feed())
    t = NOW
    while t < now:  # run the market forward so releases are applied and daily bars recorded
        t = min(t + 3 * 3600, now)
        f.step(t)
    h = f.research
    items = h.market_news(100, now=now)["items"] + [i for s in ("ORBT", "AUTO", "MKTX") for i in h.news(s, 50, now)["items"]]
    assert items
    seen_categories = set()
    for i in items:
        assert i["created_at"] <= now and i["event"]["ts"] <= now and i["source"] == "Simulated" and i["simulated"]
        assert set(i["symbols"]) <= set(f.info) and i["url"] == ""
        assert not any(w in i["headline"] + i["summary"] for w in REAL_WORDS)
        assert i["summary"].count(". ") >= 1 and i["summary"].endswith(".")
        seen_categories.add(i["category"])
        ev = i["event"]
        if ev["kind"] == "earnings":
            sym, k = ev["id"].rsplit("-", 1)
            assert h.earnings_record(sym, int(k), now)["status"] == "reported"
        if ev["kind"] == "economic_release":
            assert any(e["id"] == ev["id"] for e in macro.releases(h.seed, ev["ts"] - 1, now))
        if ev["kind"] == "price_move":
            sym, day = ev["id"].split("-", 1)
            rows = f.store.daily[sym]
            j = next(n for n, r in enumerate(rows) if date(1970, 1, 1) + timedelta(days=r[0] // 86400) == date.fromisoformat(day))
            assert abs(rows[j][4] / rows[j - 1][4] - 1 - ev["return"]) < 1e-12
        if ev["kind"] == "rating":
            assert any(c["id"] == ev["id"] for c in h.rating_changes(i["symbols"][0], cal.ny_date(now)))
    assert {"rating", "macro"} <= seen_categories or now == NOW
    if now > NOW + 86400 * 80:
        assert "earnings" in seen_categories


def test_news_is_newest_first_and_category_filter_works():
    h = feed().research
    items = h.market_news(60, now=NOW)["items"]
    assert [i["created_at"] for i in items] == sorted((i["created_at"] for i in items), reverse=True)
    only = h.market_news(60, category="rating", now=NOW)["items"]
    assert only and all(i["category"] == "rating" for i in only)


# ---- routes --------------------------------------------------------------------------------------------------
def client(f=None, start=None):
    f = f or feed()
    ex, clock = _exchange(f, start=start)
    return TestClient(create_app(ex, run_loop=False)), ex, clock


def test_research_routes_on_simulated_data():
    c, ex, clock = client()
    src = c.get("/api/research/sources").json()
    assert src["reason"] is None and src["statements"] == "Simulated" and "Simulated data" in src["note"]
    n = c.get("/api/research/news?limit=5").json()
    assert n["available"] and n["source"] == "Simulated" and 0 < len(n["items"]) <= 5 and n["as_of"] == ex.now()
    s = c.get("/api/research/statements/NVLX").json()
    assert s["data"]["quarterly"][0]["income"]["revenue"] > 0 and len(s["data"]["annual"]) == 3
    assert c.get("/api/research/statements/TECX").json()["data"]["kind"] == "fund"
    e = c.get("/api/research/economic-calendar?start=2026-11-01&end=2026-11-30").json()
    assert [r["kind"] for r in e["items"]] == ["jobs", "cpi", "retail", "rate"]
    ec = c.get("/api/research/earnings-calendar?start=2027-01-20&end=2027-02-28").json()
    assert {r["symbol"] for r in ec["items"]} == set(STOCKS) and all(r["status"] == "upcoming" for r in ec["items"])
    cal_ = c.get("/api/research/calendar/UTIL").json()
    assert cal_["dividends"]["items"] and cal_["dividends"]["items"][0]["type"] == "cash_dividend"
    f = c.get("/api/research/fundamentals/TECX").json()["data"]
    assert f["holdings"][0]["weight_pct"] > 0
    assert c.get("/api/research/economic-calendar?start=2026-12-01&end=2026-11-01").status_code == 400
    assert c.get("/api/research/earnings-calendar?start=2026-01-01&end=2027-12-01").status_code == 400
    assert c.get("/api/research/statements/ZZZZ").status_code == 404
    # the hub follows the exchange clock
    clock.advance(86400 * 90)
    ex.tick()
    assert c.get("/api/research/news?limit=1").json()["as_of"] == ex.now()


def test_new_routes_on_a_feed_without_simulated_research_say_unavailable():
    f = SimFeed()
    f.set_quote("ABC", "9.99", "10.00")
    ex = Exchange(f, SimClock(NOW))
    ex.tick()
    c = TestClient(create_app(ex, run_loop=False))
    for path in ("news", "economic-calendar", "earnings-calendar", "statements/ABC"):
        r = c.get(f"/api/research/{path}").json()
        assert r["available"] is False and r["reason"]


def test_replay_routes_serve_research_on_the_simulated_clock(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERDESK_DATA", str(tmp_path))
    c, ex, clock = client()
    r = c.post("/api/replay/start", json={"session": SAMPLES[2][0]})
    assert r.status_code == 200, r.text
    rp = c.app.state.ctx.replay
    n = c.get("/api/research/news?limit=5").json()
    assert n["as_of"] == rp.now() and n["source"] == "Simulated"
    day = cal.ny_date(rp.now()).isoformat()
    e = c.get("/api/research/economic-calendar").json()
    assert all(r["actual"] is None for r in e["items"] if r["date"] > day)


# ---- notification centre: earnings of held symbols through the calendar alert ---------------------------
def test_calendar_alert_notifies_upcoming_earnings_of_held_symbols():
    f = feed()
    ts, k = first_release(f.research, "ORBT")  # after the close on 2027-02-01
    ex, clock = _exchange(f, start=ts - 6 * 86400)
    a = ex.create_account("t", "margin", D(100000)).id
    ex.submit(a, {"symbol": "ORBT", "side": "buy", "qty": 10})
    al = ex.alerting.create({"kind": "calendar", "account_id": a, "events": ["earnings"], "days_before": 3,
                             "repeat": True})
    for _ in range(5):
        clock.advance(86400)
        ex.tick()
    notes = [n for n in ex.alerting.notices.logs[a] if n.get("alert_id") == al["id"]]
    assert [n["title"] for n in notes] == ["Upcoming earnings: ORBT"]  # once, three days ahead, held only
    assert "reports Q4 2026 results after close in 3 days (2027-02-01)" in notes[0]["text"]
    assert "Simulated data" in notes[0]["text"]
    with pytest.raises(ValueError, match="dividend, split, earnings"):
        ex.alerting.create({"kind": "calendar", "account_id": a, "events": ["mergers"]})
