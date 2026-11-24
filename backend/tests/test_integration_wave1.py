"""Interactions between the reports, data, lifecycle and orders branches (integration wave 1).

Each test covers one interaction named in the integration brief:
(a) lifecycle pre-trade rules on the new entry paths, and margin-call liquidation vs the
    "closing that uncovers a short option is risk-checked" rule
(b) splits cancel the new order kinds
(c) tax-lot methods with splits and dividends
(d) settlement and day-trade counting see multi-leg fills
(e) replay sandboxes: lifecycle state, new order types, live state untouched
(f) provider option quotes in multi-leg pricing and preview
(g) save/load of an account using everything; a state file written by origin/main loads
"""
import json
import os
import shutil
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.analytics import journal
from app.analytics.trades import gains_report
from app.api.server import create_app
from app.core import calendar as cal
from app.core.clock import SimClock
from app.core.models import Quote, to_plain
from app.exchange import Exchange, OrderRejected
from app.ledger.ledger import REALIZED
from app.lifecycle import settings as cfg
from app.lifecycle.actions import dividend, split
from app.marketdata.feed import SimFeed
from app.marketdata.replay.control import ReplayController
from app.marketdata.replay.format import SUFFIX
from app.marketdata.replay.library import BUNDLED_DIR
from app.marketdata.replay.samples import SAMPLES
from app.marketdata.synthetic import SyntheticFeed
from app.options import chain
from app.options.symbology import build
from helpers import D, T0, make_exchange, move

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
# T0 is Tuesday 2026-11-24 06:00 UTC; the first day roll ("always" session mode) is into Wednesday.
EX1 = date(2026, 11, 25)
NEAR = chain.expirations(cal.ny_date(T0))[0]
FAR = chain.expirations(cal.ny_date(T0))[-1]  # monthly, well after every test's day rolls


def opt(kind, strike, expiry=NEAR, underlying="ABC"):
    return build(underlying, expiry, kind, D(str(strike)))


def leg(symbol, side, ratio=1):
    return {"symbol": symbol, "side": side, "ratio": ratio}


def acct(ex, type="margin", cash="100000", **settings):
    a = ex.create_account("t", type, D(cash))
    a.settings.update(settings)
    return a.id


def next_day(ex, clock, days=1):
    clock.advance(86400 * days)
    ex.tick()


def round_trip(ex, a, qty=10):
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": qty})
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": qty})


def ledger_ok(account):
    assert sum(account.ledger.balances.values()) == 0
    assert {k: v for k, v in account.ledger.recompute().items() if v} == \
           {k: v for k, v in account.ledger.balances.items() if v}
    for sym in account.lots:
        assert account.ledger.balance(f"pos:{sym}") == account.cost_basis(sym)


def gains_match_ledger(account):
    assert gains_report(account)["totals"]["gain"] == -account.ledger.balance(REALIZED) == account.realized


C100, C105, C110 = opt("C", 100), opt("C", 105), opt("C", 110)
SPREAD_OPEN = [leg(C100, "buy"), leg(C105, "sell")]
SPREAD_CLOSE = [leg(C100, "sell"), leg(C105, "buy")]


# ---- (a) lifecycle pre-trade rules on the new entry paths --------------------------------------
def test_a_lifecycle_rules_run_for_every_order_of_oco_oto_and_multileg_requests():
    # Structural on purpose: the pattern day trader rule itself is being changed on the lifecycle
    # branch (it will block opening rather than closing orders), so this checks that the pre-trade
    # hook sees every order a request can fill now, and that its answer rejects the whole request.
    ex, feed, clock = make_exchange(ABC="100.00", XYZ="50.00")
    a = acct(ex)
    seen, block = [], set()
    real = ex.lifecycle.pre_trade

    def spy(account, order, worst):
        seen.append((order.symbol, order.side))
        return f"blocked {order.symbol} {order.side}" if (order.symbol, order.side) in block else real(account, order, worst)

    ex.lifecycle.pre_trade = spy
    oco = {"oco": [{"symbol": "ABC", "side": "buy", "qty": 5, "type": "limit", "limit_price": "80", "tif": "gtc"},
                   {"symbol": "XYZ", "side": "sell", "qty": 5, "type": "limit", "limit_price": "60", "tif": "gtc"}]}
    oto = {"symbol": "XYZ", "side": "buy", "qty": 5, "type": "limit", "limit_price": "40", "tif": "gtc",
           "triggers": [{"symbol": "ABC", "side": "buy", "qty": 3, "type": "limit", "limit_price": "80"}]}
    multi = {"legs": SPREAD_OPEN, "qty": 1}
    for req, expect in ((oco, [("ABC", "buy"), ("XYZ", "sell")]), (oto, [("XYZ", "buy")]),
                        (multi, [(C100, "buy"), (C105, "sell")])):
        for call in (lambda: ex.preview(a, req), lambda: ex.submit(a, req)):
            seen.clear()
            call()
            assert seen == expect
    # a rejection of any one member or leg rejects the whole request, in submit and in preview
    block.update({("XYZ", "sell"), (C105, "sell")})
    n = len(ex.engine.orders)
    with pytest.raises(OrderRejected, match="blocked XYZ sell"):
        ex.submit(a, oco)
    assert ex.preview(a, oco)["reason"] == "blocked XYZ sell"
    with pytest.raises(OrderRejected, match=f"blocked {C105} sell"):
        ex.submit(a, multi)
    assert ex.preview_multileg(a, multi)["reason"] == f"blocked {C105} sell"
    assert not ex.preview(a, multi)["ok"]
    assert len(ex.engine.orders) == n  # nothing was placed


def test_a_settled_cash_rule_runs_for_oco_and_multileg_orders():
    ex, feed, clock = make_exchange()
    a = acct(ex, "cash", "12000")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 50})
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 50})  # 4,999.50 unsettled, 7,000.00 settled
    settled = "Cash accounts can only buy with settled cash"
    oco = {"oco": [{"symbol": "ABC", "side": "buy", "qty": 75, "type": "limit", "limit_price": "100", "tif": "gtc"},
                   {"symbol": "ABC", "side": "buy", "qty": 70, "type": "limit", "limit_price": "99", "tif": "gtc"}]}
    with pytest.raises(OrderRejected, match=settled):
        ex.submit(a, oco)
    assert settled in ex.preview(a, oco)["reason"]
    # a buy-write (100 shares and one covered call) costs about 9,700: under total cash, over settled cash
    buy_write = {"legs": [leg("ABC", "buy", 100), leg(C100, "sell")], "qty": 1}
    with pytest.raises(OrderRejected, match=settled):
        ex.submit(a, buy_write)
    assert settled in ex.preview_multileg(a, buy_write)["reason"]
    small = {"oco": [dict(x, qty=10, limit_price=px) for x, px in zip(oco["oco"], ("90", "89"))]}
    assert [o.status for o in ex.submit(a, small)] == ["working", "working"]


def test_a_margin_call_liquidation_can_close_what_covers_a_short_option():
    ex, feed, clock = make_exchange()
    move(ex, feed, clock, "ABC", "100", bid_size=100000, ask_size=100000)
    a = acct(ex)
    call = opt("C", 150, FAR)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1900})  # cash -90,000
    ex.submit(a, {"symbol": call, "side": "sell", "qty": 19})  # covered by the shares
    move(ex, feed, clock, "ABC", "60", bid_size=100000, ask_size=100000)
    s = ex.summary(a)
    assert s["margin_call"] and s["margin_call_since"] is not None
    # Liquidation goes straight to the engine, so the orders branch's rule that a "closing" order which
    # uncovers a short option must pass the full risk check never stops it from closing positions.
    clock.advance(48 * 3600)
    feed.set_quote("ABC", D("59.99"), D("60"), D("60"), bid_size=100000, ask_size=100000)
    ex.tick()
    acc = ex.account(a)
    s = ex.summary(a)
    assert not s["margin_call"] and s["equity"] >= s["maintenance_requirement"]
    assert acc.position("ABC") < 1900 and acc.position(call) == 0  # calls bought back, then shares sold
    liq = [o for o in ex.engine.orders.values() if o.role == "liquidation"]
    assert {o.symbol for o in liq} == {"ABC", call} and all(o.status == "filled" for o in liq)
    assert any(e[1] == "liquidation" for e in acc.life.log)
    # lifecycle review rule: the call clears once equity has stayed above maintenance for a while
    for _ in range(2):  # the first tick starts the timer, the second (300 s later) clears the call
        clock.advance(300)
        feed.set_quote("ABC", D("59.99"), D("60"), D("60"), bid_size=100000, ask_size=100000)
        ex.tick()
    assert ex.summary(a)["margin_call_since"] is None
    ledger_ok(acc)


def test_a_margin_call_liquidation_never_leaves_covered_calls_uncovered():
    ex, feed, clock = make_exchange()
    move(ex, feed, clock, "ABC", "100", bid_size=100000, ask_size=100000)
    a = acct(ex)
    call = opt("C", 150, FAR)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1900})
    ex.submit(a, {"symbol": call, "side": "sell", "qty": 19})
    move(ex, feed, clock, "ABC", "60", bid_size=100000, ask_size=100000)
    clock.advance(48 * 3600)
    feed.set_quote("ABC", D("59.99"), D("60"), D("60"), bid_size=100000, ask_size=100000)
    ex.tick()
    acc = ex.account(a)
    assert -acc.position(call) * 100 <= acc.position("ABC")


# ---- (b) splits cancel the new order kinds -------------------------------------------------------
def test_b_split_cancels_new_order_kinds_and_whole_linked_groups():
    ex, feed, clock = make_exchange(ABC="100.00", XYZ="50.00")
    a = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
    gtd_day = (EX1 + timedelta(days=5)).isoformat()
    [moo] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "market_on_open"})
    [loc] = ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 10, "type": "limit_on_close", "limit_price": "500"})
    [gtd] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "limit_price": "80",
                          "tif": "gtd", "good_till_date": gtd_day})
    oco = ex.submit(a, {"oco": [{"symbol": "ABC", "side": "buy", "qty": 5, "type": "limit", "limit_price": "80", "tif": "gtc"},
                                {"symbol": "XYZ", "side": "buy", "qty": 5, "type": "limit", "limit_price": "40", "tif": "gtc"}]})
    oto = ex.submit(a, {"symbol": "XYZ", "side": "buy", "qty": 5, "type": "limit", "limit_price": "40", "tif": "gtc",
                        "triggers": [{"symbol": "ABC", "side": "buy", "qty": 3, "type": "limit", "limit_price": "80"}]})
    bracket = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 5, "type": "limit", "limit_price": "80", "tif": "gtc",
                            "take_profit": "120", "stop_loss": "70"})
    [ml] = ex.submit(a, {"legs": SPREAD_OPEN, "qty": 1, "type": "limit", "limit_price": "0.05",
                         "price_effect": "debit", "tif": "gtc"})
    [other] = ex.submit(a, {"symbol": "XYZ", "side": "buy", "qty": 5, "type": "limit", "limit_price": "40",
                            "tif": "gtd", "good_till_date": gtd_day})
    assert ml.status == "working" and oto[1].status == "held"
    feed.schedule(split("ABC", EX1, 2))
    next_day(ex, clock)
    # The closing auction at the end of the old day runs before the split (limit not reached: expired).
    assert loc.status == "expired"
    reason = "Canceled because of the 2-for-1 stock split of ABC effective 2026-11-25"
    for o in [moo, gtd, ml, *oco, oto[0], bracket[0]]:
        assert o.status == "canceled" and o.reason == reason, (o.id, o.type, o.status, o.reason)
    # held children end with their entry; none is left working
    for o in [oto[1], *bracket[1:]]:
        assert not o.active
    assert other.status == "working"  # not linked to ABC
    assert ex.account(a).position("ABC") == 200
    ledger_ok(ex.account(a))


# ---- (c) tax-lot methods with splits and dividends -----------------------------------------------
@pytest.mark.parametrize("method", ["fifo", "lifo", "highest_cost"])
def test_c_lot_methods_with_splits_and_dividends_keep_the_ledger_and_gains_in_step(method):
    ex, feed, clock = make_exchange()
    a = acct(ex)
    acc = ex.account(a)
    acc.lot_method = method
    for px, qty in (("100", 11), ("110", 10), ("90", 10)):
        move(ex, feed, clock, "ABC", px)
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": qty})
    feed.schedule(dividend("ABC", EX1, EX1 + timedelta(days=2), "0.40"))
    feed.schedule(split("ABC", EX1 + timedelta(days=1), 3, 2))  # 31 shares -> 46.5: cash in lieu
    next_day(ex, clock)
    next_day(ex, clock)
    assert acc.position("ABC") == 46
    assert any(c.get("kind") == "cash_in_lieu" for c in acc.closed)
    ledger_ok(acc)
    gains_match_ledger(acc)
    move(ex, feed, clock, "ABC", "70")
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 20})
    next_day(ex, clock)  # dividend paid
    assert [t.kind for t in acc.ledger.transactions].count("dividend") == 1
    move(ex, feed, clock, "ABC", "75")
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 7})
    ledger_ok(acc)
    gains_match_ledger(acc)
    rows = [r for r in gains_report(acc)["rows"] if r["kind"] == "trade"]
    assert {r["lot_method"] for r in rows} == {method}
    if method == "lifo":  # the newest lot (bought at 90, now 60 after the split) closes first
        assert rows[0]["open_price"] == D("60")
    if method == "highest_cost":  # the 110 lot (73.33... after the split) closes first
        assert rows[0]["open_price"] == (D("110") * 2 / 3).quantize(D("0.00000001"))


# ---- (d) settlement and day-trade counting see multi-leg fills --------------------------------------
def test_d_settlement_and_day_trades_count_each_multileg_leg():
    ex, feed, clock = make_exchange()
    c = acct(ex, "cash", "20000")
    [o] = ex.submit(c, {"legs": [leg("ABC", "buy", 100), leg(C100, "sell")], "qty": 1})  # buy-write
    stock_fill, call_fill = [f for f in ex.account(c).fills if f.order_id == o.id]
    life = ex.account(c).life
    assert [e[2] for e in life.unsettled] == [call_fill.id]  # only the sold leg brings cash to settle
    assert ex.summary(c)["unsettled_cash"] == call_fill.price * 100 - call_fill.fee
    next_day(ex, clock)
    assert ex.summary(c)["unsettled_cash"] == 0

    m = acct(ex, "margin", "100000")
    [credit] = ex.submit(m, {"legs": SPREAD_CLOSE, "qty": 1})  # sell C100, buy C105: a credit spread
    short = [f for f in ex.account(m).fills if f.order_id == credit.id][0]
    assert ex.account(m).life.unsettled[-1][2] == short.id
    assert ex.summary(m)["day_trades"] == 0
    [close] = ex.submit(m, {"legs": SPREAD_OPEN, "qty": 1})
    assert close.status == "filled" and not ex.account(m).positions()
    # both legs closed positions opened today. Lifecycle review rule: day trades are counted per opening
    # lot, so the two legs (two lots opened by one order, each a fill) are two day trades.
    assert ex.summary(m)["day_trades"] == 2


# ---- (e) replay sandboxes -----------------------------------------------------------------------------
def sample(i=0):
    return os.path.join(BUNDLED_DIR, SAMPLES[i][0] + SUFFIX)


def test_e_replay_sandbox_has_lifecycle_and_new_order_types_follow_the_simulated_clock():
    c = ReplayController(sample(0))
    aid = list(c.ex.accounts)[0]
    acc = c.ex.account(aid)
    assert acc.settings == cfg.defaults() and acc.life.unsettled == [] and acc.lot_method == "fifo"
    assert {"settled_cash", "day_trades", "liquidation_at"} <= set(c.ex.summary(aid))
    c.step(600)
    day = cal.ny_date(c.now()).isoformat()
    last = c.feed.quote("NVLX").last
    [moc] = c.ex.submit(aid, {"symbol": "NVLX", "side": "buy", "qty": 3, "type": "market_on_close"})
    [gtd] = c.ex.submit(aid, {"symbol": "NVLX", "side": "buy", "qty": 1, "type": "limit",
                              "limit_price": str((last / 2).quantize(D("0.01"))), "tif": "gtd", "good_till_date": day})
    oco = c.ex.submit(aid, {"oco": [
        {"symbol": "NVLX", "side": "buy", "qty": 2, "type": "limit", "limit_price": str((last / 2).quantize(D("0.01"))), "tif": "gtc"},
        {"symbol": "ORBT", "side": "buy", "qty": 2, "type": "market", "tif": "day"}]})
    assert oco[1].status == "filled" and oco[0].status == "canceled"  # filled at once, the other side canceled
    round_trip_at = c.now()
    c.ex.submit(aid, {"symbol": "NVLX", "side": "buy", "qty": 5})
    c.ex.submit(aid, {"symbol": "NVLX", "side": "sell", "qty": 5})
    assert c.ex.summary(aid)["day_trades"] == 1 and acc.life.unsettled and acc.life.unsettled[-1][0] > day
    assert moc.status == "working" and gtd.status == "working"
    # The simulated clock jumps hours at a time; lifecycle state follows the simulated date, not the wall
    # clock, and default rules (T+1, PDT, margin calls) neither fire spuriously nor lose state.
    c.seek(c.start + 5 * 3600)
    assert c.ex.lifecycle.last_date == date.fromisoformat(day)
    assert c.ex.summary(aid)["day_trades"] == 1 and acc.life.unsettled[-1][0] > day
    c.seek(c.end)
    assert c.ex.lifecycle.last_date == date.fromisoformat(day) and c.ex.summary(aid)["day_trades"] == 1
    assert not acc.life.margin_call_since
    assert not [t for t in acc.ledger.transactions if t.kind in ("margin_call", "liquidation", "interest")]
    close = cal.close_ts(date.fromisoformat(day))
    assert moc.status == "filled" and moc.updated_at == close
    assert gtd.status == "expired" and gtd.updated_at == close
    assert [f.ts for f in acc.fills if f.order_id == moc.id] == [close] and round_trip_at < close
    ledger_ok(acc)


def live_client(tmp_path):
    import time
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()), data_dir=str(tmp_path / "live"))
    ex.create_account("Margin", "margin")
    ex.create_account("Cash", "cash", D("25000"))
    ex.tick()
    return TestClient(create_app(ex, run_loop=False)), ex


def snapshot(ex):
    return json.dumps({"accounts": [a.to_plain() for a in ex.accounts.values()],
                       "orders": [to_plain(o) for o in ex.engine.orders.values()],
                       "watchlists": ex.watchlists, "alerts": ex.alerts,
                       "lifecycle": ex.lifecycle.to_plain()}, sort_keys=True)


def test_e_entering_and_leaving_replay_leaves_live_state_byte_identical(tmp_path):
    c, live = live_client(tmp_path)
    m, cash = list(live.accounts)
    last = live.feed.quote("NVLX").last
    low = str((last / 2).quantize(D("0.01")))
    good_till = (date.fromtimestamp(live.now()) + timedelta(days=3)).isoformat()
    # live state uses every branch's features
    assert c.put(f"/api/accounts/{m}/settings", json={"margin_interest_rate": "6.5"}).status_code == 200
    assert c.put(f"/api/accounts/{m}/lot-method", json={"method": "lifo"}).status_code == 200
    first = c.post(f"/api/accounts/{m}/orders", json={"symbol": "NVLX", "side": "buy", "qty": 3}).json()[0]["id"]
    assert c.post(f"/api/accounts/{m}/journal", json={"target_type": "order", "target_id": first, "note": "live note"}).status_code == 200
    c.post(f"/api/accounts/{m}/orders", json={"symbol": "NVLX", "side": "buy", "qty": 1, "type": "limit",
                                              "limit_price": low, "tif": "gtd", "good_till_date": good_till})
    c.post(f"/api/accounts/{m}/orders", json={"oco": [
        {"symbol": "NVLX", "side": "buy", "qty": 1, "type": "limit", "limit_price": low, "tif": "gtc"},
        {"symbol": "ORBT", "side": "buy", "qty": 1, "type": "limit", "limit_price": "1", "tif": "gtc"}]})
    c.post(f"/api/accounts/{cash}/orders", json={"symbol": "NVLX", "side": "buy", "qty": 2})
    c.post(f"/api/accounts/{cash}/orders", json={"symbol": "NVLX", "side": "sell", "qty": 2})
    live.save(force=True)
    before_mem = snapshot(live)
    before_disk = open(tmp_path / "live" / "state.json", "rb").read()

    r = c.post("/api/replay/start", json={"session": SAMPLES[0][0], "account_type": "margin"})
    assert r.status_code == 200
    sid = c.get("/api/accounts").json()[0]["id"]
    assert sid == "A9001"
    s = c.get(f"/api/accounts/{sid}/settings").json()
    assert s and c.put(f"/api/accounts/{sid}/settings", json={"pattern_day_trader": False}).status_code == 200
    c.post("/api/replay/step", json={"seconds": 120})
    r = c.post(f"/api/accounts/{sid}/orders", json={"symbol": "NVLX", "side": "buy", "qty": 2, "type": "market_on_close"})
    assert r.status_code == 200 and r.json()[0]["status"] == "working"
    exp = c.get("/api/options/expirations/NVLX").json()[0]["date"]
    rows = c.get("/api/options/chain/NVLX", params={"expiry": exp}).json()["rows"]
    low, mid = rows[len(rows) // 2 - 1], rows[len(rows) // 2]  # sell the at-the-money call: it has a bid
    r = c.post(f"/api/accounts/{sid}/multileg", json={"legs": [leg(low["call"]["symbol"], "buy"), leg(mid["call"]["symbol"], "sell")], "qty": 1})
    assert r.status_code == 200 and r.json()["status"] == "filled", r.text
    assert c.put(f"/api/accounts/{sid}/lot-method", json={"method": "highest_cost"}).status_code == 200
    c.post("/api/replay/seek", json={"offset": 1800})
    assert c.post("/api/replay/exit").json() == {"active": False}

    assert snapshot(live) == before_mem
    assert open(tmp_path / "live" / "state.json", "rb").read() == before_disk
    live.save(force=True)  # nothing changed, so saving again writes the same bytes
    assert open(tmp_path / "live" / "state.json", "rb").read() == before_disk


# ---- (f) provider option quotes ---------------------------------------------------------------------------
class ProviderFeed(SimFeed):
    """A test feed that has provider quotes for some option contracts (like the Alpaca feed)."""
    name = "testprovider"

    def __init__(self):
        super().__init__()
        self.options = {}

    def option_quote(self, symbol):
        return self.options.get(symbol)


def test_f_multileg_pricing_and_preview_use_provider_option_quotes_when_present():
    clock, feed = SimClock(T0), ProviderFeed()
    ex = Exchange(feed, clock)
    feed.set_quote("ABC", D("99.99"), D("100"), D("100"))
    ex.tick()
    a = acct(ex)
    model = {s: ex.quote(s) for s in (C100, C105, C110)}
    pq = lambda s, b, k: Quote(symbol=s, bid=D(b), ask=D(k), bid_size=D(10), ask_size=D(10), last=D(k))
    feed.options = {C100: pq(C100, "7.10", "7.30"), C105: pq(C105, "4.00", "4.20")}
    assert ex.quote(C100).ask == D("7.30") and ex.quote(C110) == model[C110]
    assert (ex.quote_source(C100), ex.quote_source(C110), ex.quote_source("ABC")) == ("testprovider", "model", "testprovider")

    pre = ex.preview_multileg(a, {"legs": SPREAD_OPEN, "qty": 2})
    assert pre["net_price"] == D("3.30") and pre["price_effect"] == "debit"  # 7.30 - 4.00
    assert [l["estimated_price"] for l in pre["legs"]] == [D("7.30"), D("4.00")]
    assert [l["source"] for l in pre["legs"]] == ["testprovider", "testprovider"]
    assert not any("model prices" in w for w in pre["warnings"])
    assert ex.preview(a, {"symbol": C100, "side": "buy", "qty": 1})["estimated_price"] == D("7.30")

    c105_far = opt("C", 105, FAR)  # no provider quote, and a model bid above zero
    mixed = ex.preview_multileg(a, {"legs": [leg(C100, "buy"), leg(c105_far, "sell")], "qty": 1})
    assert [l["source"] for l in mixed["legs"]] == ["testprovider", "model"] and mixed["simulated"]
    assert any("Some legs use simulated model prices" in w for w in mixed["warnings"])
    assert mixed["legs"][1]["estimated_price"] == ex.quote(c105_far).bid > 0

    [o] = ex.submit(a, {"legs": SPREAD_OPEN, "qty": 2})
    assert o.status == "filled" and o.avg_fill_price == D("3.30")
    assert [(f.symbol, f.price) for f in ex.account(a).fills if f.order_id == o.id] == [(C100, D("7.30")), (C105, D("4.00"))]
    # without provider data the same order is priced by the model again
    feed.options = {}
    pre = ex.preview_multileg(a, {"legs": SPREAD_OPEN, "qty": 1})
    assert pre["legs"][0]["estimated_price"] == ex.quote(C100).ask and pre["legs"][0]["source"] == "model"
    assert "Option prices are simulated model prices, not market data." in pre["warnings"]


# ---- (g) persistence -----------------------------------------------------------------------------------------
def test_g_save_and_load_an_account_that_uses_everything(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path), ABC="100.00", XYZ="50.00")
    a = acct(ex, "margin", "100000", margin_interest_rate="6.25", dividend_reinvestment=True)
    c = acct(ex, "cash", "20000")
    ex.account(a).lot_method = "highest_cost"
    journal.add(ex.account(a), "order", "O1", "first note", ["swing"], ex.now())
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 31})
    move(ex, feed, clock, "ABC", "104")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 12})  # a day trade, with highest-cost lots
    ex.submit(a, {"legs": SPREAD_OPEN, "qty": 2})
    ex.submit(a, {"symbol": "XYZ", "side": "sell", "qty": 40})  # short: borrow fee
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 5, "type": "market_on_open"})
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 5, "type": "limit", "limit_price": "80", "tif": "gtd",
                  "good_till_date": (EX1 + timedelta(days=9)).isoformat()})
    ex.submit(a, {"oco": [{"symbol": "ABC", "side": "sell", "qty": 5, "type": "limit", "limit_price": "150", "tif": "gtc"},
                          {"symbol": "ABC", "side": "sell", "qty": 5, "type": "stop", "stop_price": "80", "tif": "gtc"}]})
    ex.submit(a, {"symbol": "XYZ", "side": "buy", "qty": 5, "type": "limit", "limit_price": "40", "tif": "gtc",
                  "triggers": [{"type": "limit", "limit_price": "60"}]})
    ex.submit(a, {"legs": [leg(C100, "buy"), leg(C110, "sell")], "qty": 1, "type": "limit", "limit_price": "0.05",
                  "price_effect": "debit", "tif": "gtc"})
    ex.submit(c, {"symbol": "ABC", "side": "buy", "qty": 10})
    ex.submit(c, {"symbol": "ABC", "side": "sell", "qty": 4})  # unsettled
    feed.schedule(dividend("ABC", EX1, EX1 + timedelta(days=5), "0.30"))
    next_day(ex, clock)  # interest/borrow accrual, ex-dividend recorded, auctions run
    ex.submit(c, {"symbol": "ABC", "side": "sell", "qty": 2})
    acc = ex.account(a)
    assert acc.life.pending_dividends and acc.life.accrued_borrow > 0 and acc.life.day_trades
    assert ex.account(c).life.unsettled and acc.journal and acc.closed
    assert any(o.legs for o in ex.engine.orders.values() if o.active)
    assert any(o.status == "held" for o in ex.engine.orders.values())
    ex.save(force=True)
    state = lambda e: json.dumps({"accounts": [x.to_plain() for x in e.accounts.values()],
                                  "orders": [to_plain(o) for o in e.engine.orders.values()],
                                  "lifecycle": e.lifecycle.to_plain()}, sort_keys=True)
    before, summaries = state(ex), {x: ex.summary(x) for x in (a, c)}
    gains = gains_report(acc)["totals"]

    ex2 = Exchange(SimFeed(), SimClock(clock.now()), data_dir=str(tmp_path))
    assert ex2.load()
    for sym, px in (("ABC", "104"), ("XYZ", "50.00")):
        ex2.feed.set_quote(sym, D(px) - D("0.01"), D(px), D(px))
    assert state(ex2) == before
    ex2.tick()
    assert {x: ex2.summary(x) for x in (a, c)} == summaries
    assert gains_report(ex2.account(a))["totals"] == gains
    assert ex2.account(a).lot_method == "highest_cost" and ex2.account(a).journal == acc.journal
    assert ex2.account(a).settings["margin_interest_rate"] == "6.25"
    ledger_ok(ex2.account(a))
    gains_match_ledger(ex2.account(a))
    # sequences continue without reusing ids
    [o] = ex2.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1})
    assert o.id not in {x["id"] for x in json.loads(before)["orders"]}


def test_g_state_file_written_by_origin_main_still_loads(tmp_path):
    # tests/fixtures/state_main/state.json was written by origin/main (b27209a): two accounts, stock and
    # option positions, a bracket whose children are working, a GTC order and an alert (no reports, lifecycle or
    # orders-branch fields anywhere).
    shutil.copy(os.path.join(FIXTURES, "state_main", "state.json"), tmp_path / "state.json")
    raw = json.loads((tmp_path / "state.json").read_text())
    assert "lifecycle" not in raw and all("lot_method" not in x and "settings" not in x for x in raw["accounts"])
    clock = SimClock(T0 + 120)
    ex = Exchange(SimFeed(), clock, data_dir=str(tmp_path))
    assert ex.load()
    ex.feed.set_quote("ABC", D("101.99"), D("102.00"), D("102.00"))
    ex.tick()
    m, c = "A0001", "A0002"
    for x in (m, c):
        acc = ex.account(x)
        assert acc.lot_method == "fifo" and acc.journal == [] and acc.settings == cfg.defaults()
        ledger_ok(acc)
        gains_match_ledger(acc)
    assert ex.summary(m)["equity"] == D("100152.05") and ex.summary(c)["equity"] == D("25020.00")
    assert ex.summary(c)["settled_cash"] == ex.account(c).cash
    orders = list(ex.engine.orders.values())
    assert all(o.legs is None and o.good_till is None for o in orders)
    assert {o.role for o in orders if o.status == "working" and o.parent_id} == {"take_profit", "stop_loss"}
    # it keeps working: new order kinds, a split and a save
    [moo] = ex.submit(m, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "market_on_open"})
    assert moo.status == "working"
    ex.feed.schedule(split("ABC", EX1, 2))
    next_day(ex, clock)
    assert ex.account(m).position("ABC") == 2 * (100 + 50 - 30)
    ledger_ok(ex.account(m))
    ex.save(force=True)
    assert json.loads((tmp_path / "state.json").read_text())["lifecycle"]["applied"]
