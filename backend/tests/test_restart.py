"""What happens on the first tick after a start: expiry settlement prices, day orders, day start equity,
equity samples and positions without prices (release-candidate review B2 and B4)."""
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.core import calendar as cal
from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata.feed import SimFeed
from app.options import chain
from app.options.symbology import build, parse

from helpers import T0

D = Decimal
INFO = {"ABC": {"symbol": "ABC", "name": "ABC", "sector": "Test", "vol": 0.3}}


class LiveLikeFeed(SimFeed):
    """Like the Alpaca feed: the symbol list is known, but there are no quotes until the provider answers,
    nothing is restored from market.json, and daily bars come from the provider (here: a dict)."""
    name = "live-like"
    simulated = False

    def __init__(self, symbols=None, closes=None, fail=False):
        super().__init__(symbols)
        self.closes = closes or {}  # date -> close
        self.fail = fail
        self.bar_calls = 0

    def bars(self, symbol, tf, limit=500):
        self.bar_calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return [{"time": int((d - date(1970, 1, 1)).total_seconds()), "open": c, "high": c, "low": c,
                 "close": c, "volume": 0} for d, c in sorted(self.closes.items())]


def _book(feed_cls=LiveLikeFeed, **kw):
    """A margin account with 100 ABC, a long 90 put and a long 90 call, the stock at 100, saved to disk."""
    tmp = tempfile.mkdtemp()
    clock, feed = SimClock(T0), feed_cls()
    ex = Exchange(feed, clock, data_dir=tmp)
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    ex.tick()
    a = ex.create_account("t", "margin", D("100000")).id
    exp = chain.expirations(cal.ny_date(T0))[0]
    put, call = build("ABC", exp, "P", D("90")), build("ABC", exp, "C", D("90"))
    for sym in (put, call, "ABC"):
        ex.submit(a, {"symbol": sym, "side": "buy", "qty": 1 if sym != "ABC" else 100})
    clock.advance(61)
    ex.tick()
    ex.save(force=True)
    return tmp, a, exp, put, call


def _restart(tmp, ts, feed, mode="always"):
    ex = Exchange(feed, SimClock(ts), data_dir=tmp, session_mode=mode)
    ex.background = lambda fn: fn()  # run the provider fetch at once (it is a thread in the app)
    assert ex.load()
    return ex


def test_expiry_waits_for_a_price_and_uses_the_close_on_the_expiry_date():
    tmp, a, exp, put, call = _book()
    feed = LiveLikeFeed(INFO, closes={exp: 100.0})
    ex = _restart(tmp, parse(put).expiry_ts + 3 * 86400, feed)
    acc = ex.account(a)
    rows = ex.positions(a)  # used to raise AttributeError for an option without an underlying price
    assert {r["symbol"] for r in rows} == {"ABC", put, call}
    assert all(r["price_unavailable"] and r["market_value"] is None for r in rows)
    ex.tick()  # no quote yet, but the provider has the close on the expiry date (100): settle on that
    assert acc.positions() == {"ABC": D(200)}  # the 90 call was exercised, the 90 put expired worthless
    kinds = sorted((f.kind, f.symbol) for f in acc.fills[-3:])
    assert kinds == [("exercise", "ABC"), ("exercise", call), ("expiration", put)]
    memos = [t.memo for t in acc.ledger.transactions[-3:]]
    assert all("ABC closed at 100.00 on " + exp.isoformat() in m for m in memos)
    assert not any(k == "expiration" for _, k, _ in acc.life.log)  # exact price: no approximate-price notice


def test_expiry_falls_back_to_the_first_price_with_a_notice_when_the_close_is_unavailable():
    tmp, a, exp, put, call = _book()
    feed = LiveLikeFeed(INFO, fail=True)
    ex = _restart(tmp, parse(put).expiry_ts + 86400, feed)
    acc = ex.account(a)
    ex.tick()  # no quote and no close: nothing may be settled against a price of zero
    assert acc.positions() == {"ABC": D(100), put: D(1), call: D(1)}
    assert any(e.get("kind") == "expiration" for e in ex.events)
    feed.set_quote("ABC", "84.99", "85.00", "85.00")
    ex.clock.advance(1)
    ex.tick()
    assert feed.bar_calls == 1
    assert acc.positions() == {}  # the put is in the money at 85: shares delivered; the call expired
    assert all("approximate price" in t.memo for t in acc.ledger.transactions[-3:])
    log = [m for _, k, m in acc.life.log if k == "expiration"]
    assert len(log) == 2 and all("approximate price" in m and "85" in m for m in log)


def test_live_feed_waits_for_the_provider_fetch_before_settling():
    tmp, a, exp, put, call = _book()
    feed = LiveLikeFeed(INFO, closes={exp: 100.0})
    ex = _restart(tmp, parse(put).expiry_ts + 86400, feed)
    started = []
    ex.background = started.append  # the fetch has not answered yet
    feed.set_quote("ABC", "49.99", "50.00", "50.00")
    ex.tick()
    assert len(started) == 1 and len(ex.account(a).positions()) == 3  # waiting, not settled at 50
    started[0]()  # the provider answers
    ex.clock.advance(1)
    ex.tick()
    assert ex.account(a).positions() == {"ABC": D(200)}


def test_simulated_feed_uses_its_daily_bar_for_the_expiry_date():
    tmp, a, exp, put, call = _book(SimFeed)
    feed = SimFeed(INFO)
    feed.store.daily["ABC"] = [[int((exp - date(1970, 1, 1)).total_seconds()), 100.0, 100.0, 100.0, 100.0, 0]]
    ex = _restart(tmp, parse(put).expiry_ts + 2 * 86400, feed)
    feed.set_quote("ABC", "79.99", "80.00", "80.00")
    ex.tick()
    assert ex.account(a).positions() == {"ABC": D(200)}


def test_running_at_the_close_settles_on_the_last_session_price():
    clock, feed = SimClock(T0), SimFeed()
    ex = Exchange(feed, clock)
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    ex.tick()
    a = ex.create_account("t", "margin", D("100000")).id
    exp = chain.expirations(cal.ny_date(T0))[0]
    call = build("ABC", exp, "C", D("95"))
    ex.submit(a, {"symbol": call, "side": "buy", "qty": 1})
    clock.set(parse(call).expiry_ts - 1)
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    ex.tick()
    clock.advance(2)
    ex.tick()
    acc = ex.account(a)
    assert acc.positions() == {"ABC": D(100)}
    assert "ABC closed at 100.00" in acc.ledger.transactions[-1].memo
    assert not acc.life.log


def test_no_equity_sample_is_recorded_while_prices_are_missing():
    tmp, a, *_ = _book()
    feed = LiveLikeFeed(INFO)
    ex = _restart(tmp, T0 + 3600, feed)
    before = list(ex.account(a).equity_history)
    ex.tick()
    assert ex.account(a).equity_history == before
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    ex.clock.advance(61)
    ex.tick()
    hist = ex.account(a).equity_history
    assert len(hist) == len(before) + 1 and D(hist[-1][1]) > D("99000")


def test_corrupt_sample_from_an_unpriced_restart_is_removed_on_load():
    tmp = tempfile.mkdtemp()
    clock, feed = SimClock(T0), SimFeed()
    ex = Exchange(feed, clock, data_dir=tmp)
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    ex.tick()
    acc = ex.create_account("t", "margin", D("100000"))
    ex.submit(acc.id, {"symbol": "ABC", "side": "buy", "qty": 900})
    for _ in range(3):
        clock.advance(61)
        ex.tick()
    good = list(acc.equity_history)
    # What the old code wrote on the first tick after a start with no prices: the cash alone.
    acc.equity_history.append([int(T0 + 3600), str(acc.cash)])
    acc.equity_history.append([int(T0 + 3661), "100010.00"])
    # A real sample that happens to equal the cash while nothing was held must stay.
    ex.save(force=True)
    ex2 = Exchange(SimFeed(INFO), SimClock(T0 + 7200), data_dir=tmp)
    ex2.load()
    assert ex2.account(acc.id).equity_history == good + [[int(T0 + 3661), "100010.00"]]


def test_a_flat_account_keeps_samples_equal_to_its_cash():
    tmp = tempfile.mkdtemp()
    clock, feed = SimClock(T0), SimFeed()
    ex = Exchange(feed, clock, data_dir=tmp)
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    ex.tick()
    acc = ex.create_account("t", "margin", D("100000"))
    acc.equity_history = [[int(T0 + 100), "100000.00"], [int(T0 + 4000), "100000.00"]]
    ex.save(force=True)
    ex2 = Exchange(SimFeed(INFO), SimClock(T0 + 7200), data_dir=tmp)
    ex2.load()
    assert len(ex2.account(acc.id).equity_history) == 2


# ---- B4: day orders and day start equity across a restart -----------------------------------------
MON_10AM = datetime(2026, 11, 23, 10, 0, tzinfo=cal.NY).timestamp()


def _day_order(mode, ts=MON_10AM, **extra):
    tmp = tempfile.mkdtemp()
    clock, feed = SimClock(ts), SimFeed()
    ex = Exchange(feed, clock, data_dir=tmp, session_mode=mode)
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    ex.tick()
    a = ex.create_account("t", "margin", D("100000")).id
    [o] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "limit_price": "90",
                        "tif": "day", **extra})
    ex.save(force=True)
    return tmp, a, o


def _start(tmp, ts, mode):
    feed = SimFeed(INFO)
    ex = Exchange(feed, SimClock(ts), data_dir=tmp, session_mode=mode)
    ex.load()
    feed.set_quote("ABC", "89.00", "89.01", "89.01")  # marketable for the old order
    ex.tick()
    return ex


def test_day_order_expires_when_the_app_was_closed_over_the_roll():
    for mode in ("always", "us"):
        tmp, a, o = _day_order(mode)
        ex = _start(tmp, MON_10AM + 2 * 86400, mode)
        o2 = ex.engine.orders[o.id]
        assert o2.status == "expired" and o2.filled_qty == 0, mode
        assert "not running" in (o2.reason or "")


def test_day_order_survives_a_restart_in_the_same_session():
    for mode in ("always", "us"):
        tmp, a, o = _day_order(mode)
        ex = _start(tmp, MON_10AM + 3600, mode)
        assert ex.engine.orders[o.id].status == "filled", mode


def test_extended_hours_day_order_ends_with_the_extended_session():
    tmp, a, o = _day_order("us", extended_hours=True)
    at_1730 = datetime(2026, 11, 23, 17, 30, tzinfo=cal.NY).timestamp()
    ex = _start(tmp, at_1730, "us")
    assert ex.engine.orders[o.id].status == "filled"  # still in its extended session
    tmp, a, o = _day_order("us", extended_hours=True)
    at_2030 = datetime(2026, 11, 23, 20, 30, tzinfo=cal.NY).timestamp()
    ex = _start(tmp, at_2030, "us")
    assert ex.engine.orders[o.id].status == "expired"


def test_day_order_end_follows_the_calendar():
    ex = Exchange(SimFeed(INFO), SimClock(T0), session_mode="us")
    from app.core.models import Order
    at = lambda *dt: datetime(*dt, tzinfo=cal.NY).timestamp()
    o = lambda ts, ext=False: Order(id="x", account_id="A", symbol="ABC", side="buy", qty=D(1), type="limit",
                                    limit_price=D(1), extended_hours=ext, created_at=ts)
    assert ex._day_order_end(o(at(2026, 11, 23, 10))) == cal.close_ts(date(2026, 11, 23))
    assert ex._day_order_end(o(at(2026, 11, 23, 2))) == cal.close_ts(date(2026, 11, 23))  # overnight: that day
    assert ex._day_order_end(o(at(2026, 11, 23, 17))) == at(2026, 11, 23, 20)  # after the close: ends at 20:00
    # Placed on Wednesday night before Thanksgiving: the next trading day is Friday (an early close).
    assert ex._day_order_end(o(at(2026, 11, 25, 21))) == cal.close_ts(date(2026, 11, 27))
    assert ex._day_order_end(o(at(2026, 11, 28, 12), ext=True)) == at(2026, 11, 30, 20)  # Saturday, extended
    always = Exchange(SimFeed(INFO), SimClock(T0))
    assert always._day_order_end(o(T0 + 5)) == (T0 // 86400 + 1) * 86400


def test_gtd_and_auction_orders_agree_after_a_restart():
    tmp = tempfile.mkdtemp()
    clock, feed = SimClock(MON_10AM), SimFeed()
    ex = Exchange(feed, clock, data_dir=tmp, session_mode="us")
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    ex.tick()
    a = ex.create_account("t", "margin", D("100000")).id
    [gtd] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "limit", "limit_price": "90",
                          "tif": "gtd", "good_till_date": "2026-11-24"})
    [moc] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "market_on_close"})
    ex.save(force=True)
    ex2 = _start(tmp, MON_10AM + 2 * 86400, "us")
    assert ex2.engine.orders[gtd.id].status == "expired"
    assert ex2.engine.orders[moc.id].status == "expired"


def test_day_start_equity_after_a_restart_on_a_later_day():
    tmp = tempfile.mkdtemp()
    clock, feed = SimClock(T0), SimFeed()
    ex = Exchange(feed, clock, data_dir=tmp)
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    ex.tick()
    acc = ex.create_account("t", "margin", D("100000"))
    ex.submit(acc.id, {"symbol": "ABC", "side": "buy", "qty": 100})
    clock.advance(16 * 3600)  # 22:00 UTC: the evening sample
    feed.set_quote("ABC", "109.99", "110.00", "110.00")
    ex.tick()
    evening = ex.summary(acc.id)["equity"]
    assert acc.equity_history[-1][1] == str(evening)
    ex.save(force=True)
    # Next morning, the stock opens at 120: day P&L is measured from the evening before.
    feed2 = LiveLikeFeed(INFO)
    ex2 = Exchange(feed2, SimClock(T0 + 86400 + 3 * 3600), data_dir=tmp)
    ex2.load()
    ex2.tick()  # no price yet: the day start waits
    feed2.set_quote("ABC", "119.99", "120.00", "120.00")
    ex2.clock.advance(1)
    ex2.tick()
    acc2 = ex2.account(acc.id)
    assert acc2.day_start_equity == evening
    assert ex2.summary(acc.id)["day_pnl"] == D("1000.00")


def test_day_start_equity_without_a_usable_sample_uses_the_current_equity_with_a_note():
    tmp = tempfile.mkdtemp()
    clock, feed = SimClock(T0), SimFeed()
    ex = Exchange(feed, clock, data_dir=tmp)
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    ex.tick()
    acc = ex.create_account("t", "margin", D("100000"))
    ex.submit(acc.id, {"symbol": "ABC", "side": "buy", "qty": 100})
    clock.advance(61)
    ex.tick()
    ex.save(force=True)
    feed2 = SimFeed(INFO)
    ex2 = Exchange(feed2, SimClock(T0 + 5 * 86400), data_dir=tmp)
    ex2.load()
    feed2.set_quote("ABC", "119.99", "120.00", "120.00")
    ex2.tick()
    assert ex2.account(acc.id).day_start_equity == ex2.summary(acc.id)["equity"]
    assert any(e.get("kind") == "day_start" for e in ex2.events)


# ---- other consumers of a missing price ---------------------------------------------------------------
def test_orders_that_add_risk_wait_for_prices_but_closing_orders_do_not():
    import pytest
    from app.exchange import OrderRejected
    tmp = tempfile.mkdtemp()
    clock, feed = SimClock(T0), SimFeed()
    ex = Exchange(feed, clock, data_dir=tmp)
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    feed.set_quote("XYZ", "49.99", "50.00", "50.00")
    ex.tick()
    a = ex.create_account("t", "margin", D("100000")).id
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 100})  # short ABC
    ex.save(force=True)
    feed2 = LiveLikeFeed({**INFO, "XYZ": {"symbol": "XYZ", "name": "XYZ", "sector": "Test", "vol": 0.3}})
    ex2 = Exchange(feed2, SimClock(T0 + 60), data_dir=tmp)
    ex2.load()
    feed2.set_quote("XYZ", "49.99", "50.00", "50.00")  # ABC has no price yet
    ex2.tick()
    with pytest.raises(OrderRejected, match="no price is available yet for ABC"):
        ex2.submit(a, {"symbol": "XYZ", "side": "buy", "qty": 1000})
    pv = ex2.preview(a, {"symbol": "XYZ", "side": "buy", "qty": 1})
    assert not pv["ok"] and "ABC" in pv["reason"]


def test_exposure_leaves_out_unpriced_positions():
    from app.analytics.trades import exposure
    rows = [{"symbol": "ABC", "qty": D(1), "market_value": None}, {"symbol": "XYZ", "qty": D(1), "market_value": D("50")}]
    out = exposure(rows, {}, D("100"))
    assert out["unpriced"] == ["ABC"] and out["totals"]["long"] == D("50")


def test_a_worthless_quoted_option_is_not_a_missing_price():
    from app.core.models import Quote

    class ZeroOptions(SimFeed):
        def option_quote(self, symbol):  # a provider quote of 0.00 / 0.00 for a far out-of-the-money option
            return Quote(symbol=symbol, bid=D(0), ask=D(0), bid_size=D(0), ask_size=D(0), last=D(0), ts=T0)

    clock, feed = SimClock(T0), ZeroOptions()
    ex = Exchange(feed, clock)
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    ex.tick()
    a = ex.create_account("t", "margin", D("100000")).id
    exp = chain.expirations(cal.ny_date(T0))[0]
    far = build("ABC", exp, "C", D("400"))
    from app.ledger.account import Lot
    ex.account(a).lots[far] = [Lot(D(1), D("0.01"), T0, D("1.00"))]
    assert ex.mark(far) == 0
    assert ex.summary(a)["prices_unavailable"] == []
    clock.advance(61)
    ex.tick()
    assert ex.account(a).equity_history  # sampled


# ---- N14: the margin-call grace period counts running, open-market time only ----------------------------
def test_margin_call_grace_does_not_run_while_the_app_is_closed():
    tmp = tempfile.mkdtemp()
    clock, feed = SimClock(T0), SimFeed()
    ex = Exchange(feed, clock, data_dir=tmp)
    feed.set_quote("ABC", "99.99", "100.00", "100.00", bid_size=100000, ask_size=100000)
    ex.tick()
    acc = ex.create_account("t", "margin", D("100000"))
    ex.submit(acc.id, {"symbol": "ABC", "side": "buy", "qty": 1900})
    clock.advance(5)
    feed.set_quote("ABC", "59.99", "60.00", "60.00", bid_size=100000, ask_size=100000)
    ex.tick()
    assert acc.life.margin_call_since is not None
    for _ in range(10):  # ten minutes of the 48 hours used
        clock.advance(60)
        ex.tick()
    ex.save(force=True)
    feed2 = SimFeed(INFO)
    ex2 = Exchange(feed2, SimClock(T0 + 49 * 3600), data_dir=tmp)
    ex2.load()
    feed2.set_quote("ABC", "59.99", "60.00", "60.00", bid_size=100000, ask_size=100000)
    ex2.tick()  # 49 hours later: the first tick does not liquidate
    acc2 = ex2.account(acc.id)
    assert acc2.positions() == {"ABC": D(1900)}
    assert acc2.life.margin_call_counted == 600
    assert "47.8 hours" in acc2.life.log[-1][2]
    s = ex2.summary(acc.id)
    assert s["grace_hours_left"] == (48 * 3600 - 600) / 3600
    assert s["liquidation_at"] == ex2.now() + 48 * 3600 - 600


# ---- N9 / N10: accrued charges in account value; the interest memo names every rate used ----------------
def test_accrued_charges_count_in_equity_and_the_memo_lists_each_rate():
    from datetime import datetime, timezone
    clock, feed = SimClock(T0), SimFeed()
    ex = Exchange(feed, clock)
    feed.set_quote("ABC", "99.99", "100.00", "100.00", bid_size=100000, ask_size=100000)
    ex.tick()
    acc = ex.create_account("t", "margin", D("100000"))
    acc.settings["margin_interest_rate"] = "7.2"
    ex.submit(acc.id, {"symbol": "ABC", "side": "buy", "qty": 1500})  # cash -50,000: 10.00 a day at 7.2%
    clock.advance(86400 * 3)
    ex.tick()  # Nov 24, 25, 26 accrued
    s = ex.summary(acc.id)
    mv = D(1500) * D("100.00")
    assert s["accrued_charges"] == D("30.00") and s["equity"] == s["cash"] + mv - D("30.00")
    pos, marks, eq, maint = ex.lifecycle._state(acc)
    assert eq == s["equity"]  # margin checks see the same account value
    acc.settings["margin_interest_rate"] = "3.6"  # 5.00 a day from Nov 27
    clock.set(datetime(2026, 12, 1, 0, 0, 30, tzinfo=timezone.utc).timestamp())
    ex.tick()
    [t] = [t for t in acc.ledger.transactions if t.kind == "interest"]
    assert t.postings[0] == ("expense:margin_interest", D("50.00"))
    assert t.memo == "Margin interest for November 2026: 50.00 (3.6% a year for 4 days, 7.2% a year for 3 days; actual/360)"
    assert ex.summary(acc.id)["accrued_charges"] == 0


# ---- N13: reset keeps settings and the tax-lot method, clears the journal, validates the cap --------------
def test_reset_keeps_settings_and_lot_method_and_validates_the_starting_cash():
    import pytest
    from fastapi.testclient import TestClient
    from app.api.server import create_app
    clock, feed = SimClock(T0), SimFeed()
    ex = Exchange(feed, clock)
    feed.set_quote("ABC", "99.99", "100.00", "100.00")
    ex.tick()
    acc = ex.create_account("t", "margin", D("100000"))
    acc.lot_method, acc.settings["margin_interest_rate"] = "lifo", "5"
    acc.journal.append({"id": "J1", "note": "x"})
    c = TestClient(create_app(ex, run_loop=False))
    r = c.post(f"/api/accounts/{acc.id}/reset", json={"starting_cash": "2000000000"})
    assert r.status_code == 400 and "1,000,000,000" in r.json()["detail"]
    r = c.post(f"/api/accounts/{acc.id}/reset", json={"starting_cash": "-5"})
    assert r.status_code == 400
    assert c.post(f"/api/accounts/{acc.id}/reset", json={"starting_cash": "5000"}).status_code == 200
    new = ex.account(acc.id)
    assert new.lot_method == "lifo" and new.settings["margin_interest_rate"] == "5" and new.journal == []
    assert new.cash == D("5000")
    with pytest.raises(ValueError):
        ex.create_account("x", "margin", D("0.001"))
