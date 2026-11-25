"""Crypto spot trading: asset model, rules, simulated coins, API, reports and the Alpaca crypto adapter.

No network: the Alpaca tests serve the documented example responses in tests/fixtures/alpaca_crypto.
"""
import csv
import io
import json
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings, strategies as st

from app.analytics.export import fills_csv, ledger_csv
from app.api.server import create_app
from app.core import assets
from app.core.clock import SimClock
from app.core.models import Quote
from app.exchange import Exchange, OrderRejected
from app.ledger.ledger import CASH, FEES
from app.marketdata.crypto_sim import CRYPTO_SYMBOLS
from app.marketdata.feed import SimFeed
from app.marketdata.http import Transport, loads
from app.marketdata.synthetic import SyntheticFeed
from app.risk import margin

D = Decimal
FIX = Path(__file__).parent / "fixtures" / "alpaca_crypto"
SAT = datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc).timestamp()  # a Saturday: US stock market closed
COIN = "SIMCOIN/USD"


def coin_info(sym=COIN, qinc="0.0001", pinc="0.01", mn="0.001"):
    return assets.crypto_info(sym, f"{sym} test coin (fictional)", qinc, pinc, mn, vol=0.8)


def make(session_mode="us", t=SAT, cash=D("100000"), price="40000.00", **coin):
    """Scripted feed with one stock (ABC) and one coin, on a weekend clock."""
    clock = SimClock(t)
    feed = SimFeed({"ABC": {"symbol": "ABC", "name": "ABC", "sector": "Test", "vol": 0.3}, COIN: coin_info(**coin)})
    ex = Exchange(feed, clock, session_mode=session_mode)
    feed.set_quote("ABC", D("99.99"), D("100.00"), D("100.00"))
    set_coin(feed, price)
    ex.tick()
    m = ex.create_account("Margin", "margin", cash)
    c = ex.create_account("Cash", "cash", cash)
    return ex, feed, clock, m, c


def set_coin(feed, price, size="5", sym=COIN, step="0.01"):
    px = D(price)
    return feed.set_quote(sym, px - D(step), px, px, bid_size=D(size), ask_size=D(size), last_size=D("0.1"))


def move(ex, feed, clock, price, seconds=1, **kw):
    clock.advance(seconds)
    set_coin(feed, price, **kw)
    ex.tick()


# ---------------------------------------------------------------------------------------------
# Asset model
# ---------------------------------------------------------------------------------------------
def test_asset_class_is_decided_by_the_symbol():
    assert assets.asset_class("SIMCOIN/USD") == assets.CRYPTO == assets.asset_class("BTC/USD")
    assert assets.asset_class("NVLX") == assets.STOCK
    assert assets.asset_class("NVLX261218C00150000") == assets.OPTION
    assert not assets.is_crypto("BTC/EUR") and not assets.is_crypto("BTCUSD") and not assets.is_crypto("")
    assert assets.base_currency("SIMDUST/USD") == "SIMDUST"


def test_increment_helpers():
    assert assets.floor_to(D("1.23456"), D("0.001")) == D("1.234")
    assert str(assets.floor_to(D("41250.0"), D("0.01"))) == "41250.00"
    assert assets.round_to(D("0.0041495"), D("0.000001")) == D("0.004150")
    assert assets.on_step(D("0.0003"), D("0.0001")) and not assets.on_step(D("0.00031"), D("0.0001"))
    assert assets.places(D("0.0001")) == 4 and assets.places(D("1")) == 0
    spec = assets.spec_from_info("X/USD", {"qty_increment": "bad", "price_increment": "0", "min_order_size": None})
    assert spec.qty_increment == spec.price_increment == spec.min_order_size == assets.FINEST


@given(st.decimals(min_value=D("0.000000001"), max_value=D("1000000"), places=9),
       st.sampled_from([D("1"), D("0.01"), D("0.0001"), D("0.00001"), D("0.000000001")]))
def test_floor_to_is_the_largest_multiple_not_above(value, step):
    f = assets.floor_to(value, step)
    assert assets.on_step(f, step) and f <= value < f + step


# ---------------------------------------------------------------------------------------------
# Simulated coins
# ---------------------------------------------------------------------------------------------
def test_simulated_coins_are_fictional_and_trade_through_the_weekend():
    feed = SyntheticFeed(seed=3, now=SAT, history_days=40, always_open=False)
    coins = {s: i for s, i in feed.symbols().items() if i.get("type") == "crypto"}
    assert sorted(coins) == sorted(CRYPTO_SYMBOLS) and all(s.startswith("SIM") for s in coins)
    assert all("fictional" in i["name"] for i in coins.values())
    first = {s: feed.quote(s).last for s in coins}
    moved = set()
    for k in range(1, 200):
        for q in feed.step(SAT + k):
            assert q.symbol in coins  # stocks are closed on a Saturday; coins keep trading
            spec = assets.spec_from_info(q.symbol, coins[q.symbol])
            for v in (q.bid, q.ask, q.last):
                assert v > 0 and assets.on_step(v, spec.price_increment)
            assert assets.on_step(q.bid_size, spec.qty_increment) and q.ask > q.bid
            if q.last != first[q.symbol]:
                moved.add(q.symbol)
    assert moved == set(coins)
    days = [r["time"] for r in feed.bars(COIN, "1d", 30)]
    assert all(b - a == 86400 for a, b in zip(days, days[1:]))  # every calendar day, weekends included


def test_old_market_file_without_coins_still_restores_the_stocks(tmp_path):
    feed = SyntheticFeed(seed=3, now=SAT, history_days=10)
    snap = feed.snapshot()
    for key in ("quotes", "daily", "minutes", "days"):
        for s in CRYPTO_SYMBOLS:
            snap[key].pop(s, None)
    snap["quotes"]["NVLX"]["last"] = "123.45"
    fresh = SyntheticFeed(seed=3, now=SAT, history_days=10)
    fresh.restore(json.loads(json.dumps(snap)))
    assert fresh.quote("NVLX").last == D("123.45") and fresh.quote(COIN) is not None
    assert fresh.step(SAT + 1)


def test_depth_uses_the_pair_increments():
    ex, feed, *_ = make()
    d = feed.depth(COIN)
    assert all(assets.on_step(D(str(r["size"])), D("0.0001")) and r["size"] > 0 for r in d["bids"] + d["asks"])
    assert d["asks"][1]["price"] - d["asks"][0]["price"] >= D("0.01")


# ---------------------------------------------------------------------------------------------
# Trading rules
# ---------------------------------------------------------------------------------------------
def test_buy_and_sell_on_a_weekend_in_us_session_mode():
    ex, feed, clock, m, c = make()
    assert ex.session() == "closed" and ex.session_for(COIN) == "regular"
    [o] = ex.submit(c.id, {"symbol": COIN, "side": "buy", "qty": "0.0125", "type": "market"})
    assert o.status == "filled" and o.avg_fill_price == D("40000.00")
    fee = assets.round_to(D("0.0125") * D("40000") * D("0.0025"), D("0.01"))
    assert o.fees == fee == D("1.25") and c.cash == D("100000") - D("500.00") - fee
    stock = ex.submit(c.id, {"symbol": "ABC", "side": "buy", "qty": "1"})[0]
    assert stock.status == "working"  # the stock order waits for Monday
    [s] = ex.submit(c.id, {"symbol": COIN, "side": "sell", "qty": "0.0125"})
    assert s.status == "filled" and c.position(COIN) == 0
    assert ex.summary(c.id)["unsettled_cash"] == 0  # crypto settles at once
    assert c.ledger.balance(FEES) == D("2.50")


@pytest.mark.parametrize("req, message", [
    ({"type": "market_on_open"}, "on-open and on-close orders do not apply to crypto"),
    ({"type": "limit_on_close", "limit_price": "40000"}, "on-open and on-close orders do not apply to crypto"),
    ({"type": "limit", "limit_price": "40000", "extended_hours": True}, "extended hours do not apply to crypto"),
    ({"tif": "fok"}, "day, good-till-canceled or immediate-or-cancel"),
    ({"tif": "gtd", "good_till_date": "2026-12-31"}, "day, good-till-canceled or immediate-or-cancel"),
    ({"type": "trailing_stop", "trail_percent": "5"}, "market, limit, stop or stop-limit"),
    ({"take_profit": "50000"}, "cannot be bracket"),
    ({"type": "limit", "limit_price": "40000.005"}, "must be a multiple of 0.01"),
    ({"qty": "0.00015"}, "must be a multiple of 0.0001"),
    ({"qty": "0.0005"}, "the minimum order for SIMCOIN/USD is 0.001"),
    ({"qty": "1", "notional": "100"}, "not both"),
    ({"qty": None, "notional": "10.001"}, "more than 2 decimal places"),
    ({"qty": None, "notional": "10", "type": "stop_limit", "stop_price": "41000", "limit_price": "41000"},
     "enter a quantity for stop orders"),
    ({"qty": None, "notional": "20"}, "minimum order"),
])
def test_rejected_crypto_orders_name_the_cause(req, message):
    ex, feed, clock, m, c = make()
    body = {"symbol": COIN, "side": "buy", "qty": "0.01", **req}
    with pytest.raises(OrderRejected, match=message):
        ex.submit(m.id, {k: v for k, v in body.items() if v is not None})
    with pytest.raises(OrderRejected, match=message):
        ex.preview(m.id, {k: v for k, v in body.items() if v is not None})


def test_linked_and_multileg_orders_are_not_for_crypto():
    ex, feed, clock, m, c = make()
    with pytest.raises(OrderRejected, match="cannot be bracket, one-cancels-other"):
        ex.submit(m.id, {"oco": [{"symbol": COIN, "side": "buy", "qty": "1", "type": "limit", "limit_price": "30000"},
                                 {"symbol": COIN, "side": "buy", "qty": "1", "type": "limit", "limit_price": "31000"}]})
    with pytest.raises(OrderRejected, match="not crypto"):
        ex.submit(m.id, {"legs": [{"symbol": COIN, "side": "buy"}, {"symbol": "ABC", "side": "sell"}], "qty": 1})
    with pytest.raises(OrderRejected, match="only be given for crypto"):
        ex.submit(m.id, {"symbol": "ABC", "side": "buy", "notional": "100"})


def test_dollar_amount_orders_round_down_to_the_quantity_increment():
    ex, feed, clock, m, c = make()
    p = ex.preview(c.id, {"symbol": COIN, "side": "buy", "notional": "1000"})
    assert p["qty"] == D("0.025") and p["asset_class"] == "crypto" and p["fee_rate"] == D("0.25")
    [o] = ex.submit(c.id, {"symbol": COIN, "side": "buy", "notional": "999.99", "type": "limit", "limit_price": "39000"})
    assert o.qty == D("0.0256") and o.status == "working"  # 999.99 / 39000 = 0.02564.. -> 0.0256


def test_no_short_selling_in_either_account_type():
    ex, feed, clock, m, c = make()
    for acct in (m, c):
        with pytest.raises(OrderRejected, match="crypto cannot be sold short: you hold none"):
            ex.submit(acct.id, {"symbol": COIN, "side": "sell", "qty": "0.01"})
    ex.submit(m.id, {"symbol": COIN, "side": "buy", "qty": "0.02"})
    ex.submit(m.id, {"symbol": COIN, "side": "sell", "qty": "0.015", "type": "limit", "limit_price": "50000", "tif": "gtc"})
    with pytest.raises(OrderRejected, match="open orders already sell 0.015"):
        ex.submit(m.id, {"symbol": COIN, "side": "sell", "qty": "0.01"})
    [o] = [x for x in ex.orders(m.id, "open")]
    with pytest.raises(OrderRejected, match="crypto cannot be sold short"):
        ex.replace(m.id, o.id, {"qty": "0.03"})
    ex.replace(m.id, o.id, {"qty": "0.02"})
    assert o.qty == D("0.02")
    assert margin.evaluate("margin", D(0), {COIN: D("-1")}, {COIN: D(1)}).reason == "crypto cannot be sold short"


def test_crypto_is_fully_paid_in_a_margin_account():
    ex, feed, clock, m, c = make(cash=D("10000"))
    before = ex.summary(m.id)
    assert before["crypto_buying_power"] < before["option_buying_power"] <= before["buying_power"] / 2
    ex.submit(m.id, {"symbol": COIN, "side": "buy", "qty": "0.1"})  # 4,000 + fee
    s = ex.summary(m.id)
    assert s["initial_requirement"] == s["maintenance_requirement"] == s["long_market_value"] == D("4000.00")
    # Crypto has no loan value: stock buying power falls by twice the cost, as if the cash were spent.
    assert s["buying_power"] == before["buying_power"] - 2 * (D("4000.00") + D("10.00"))
    # Stocks may be bought on margin with what is left ...
    ex.tick()
    clock.set(datetime(2026, 11, 30, 15, 0, tzinfo=timezone.utc).timestamp())  # Monday, market open
    feed.set_quote("ABC", D("99.99"), D("100.00"), D("100.00"))
    set_coin(feed, "40000.00")
    ex.tick()
    ex.submit(m.id, {"symbol": "ABC", "side": "buy", "qty": "100"})
    assert m.cash < 0
    # ... but crypto is never bought with borrowed money.
    with pytest.raises(OrderRejected, match="crypto is not bought on margin"):
        ex.submit(m.id, {"symbol": COIN, "side": "buy", "qty": "0.001"})
    assert ex.summary(m.id)["crypto_buying_power"] == 0
    # Selling crypto is always allowed and frees its requirement.
    ex.submit(m.id, {"symbol": COIN, "side": "sell", "qty": "0.1"})
    assert ex.summary(m.id)["initial_requirement"] == D("5000.00")


MONDAY = datetime(2026, 11, 30, 15, 0, tzinfo=timezone.utc).timestamp()  # US market open


@pytest.mark.parametrize("crypto_first", [False, True])
def test_a_crypto_purchase_does_not_change_what_a_stock_order_may_do(crypto_first):
    """Both orderings: a resting crypto buy, or the same buy filled, leave the same stock buying power."""
    ex, feed, clock, m, c = make(t=MONDAY, cash=D("100000"))
    req = {"symbol": COIN, "side": "buy", "qty": "1.4625", "type": "limit", "limit_price": "40000.00" if crypto_first else "30000.00",
           "tif": "gtc"}
    ex.submit(m.id, req)
    assert (m.position(COIN) > 0) == crypto_first
    s = ex.summary(m.id)
    bp = s["buying_power"]
    stock_qty = (bp / D("100.00")).quantize(D(1), "ROUND_FLOOR")
    assert stock_qty >= 750  # the stock buying power the summary shows is accepted ...
    ex.submit(m.id, {"symbol": "ABC", "side": "buy", "qty": str(stock_qty), "type": "limit", "limit_price": "100.00",
                     "tif": "gtc"})
    with pytest.raises(OrderRejected, match="insufficient buying power"):  # ... and no more
        ex.submit(m.id, {"symbol": "ABC", "side": "buy", "qty": "10", "type": "limit", "limit_price": "100.00", "tif": "gtc"})


def test_crypto_buying_power_is_what_a_crypto_purchase_may_spend():
    ex, feed, clock, m, c = make(t=MONDAY, cash=D("100000"))
    ex.submit(m.id, {"symbol": "ABC", "side": "buy", "qty": "800", "type": "limit", "limit_price": "99.00", "tif": "gtc"})
    s = ex.summary(m.id)
    assert s["crypto_buying_power"] == ((D("100000") - D("79200.00")) / D("1.0025")).quantize(D("0.01"), "ROUND_FLOOR")
    qty = assets.floor_to(s["crypto_buying_power"] / D("40000.00"), D("0.0001"))
    ex.submit(m.id, {"symbol": COIN, "side": "buy", "qty": str(qty), "type": "limit", "limit_price": "40000.00", "tif": "gtc"})
    with pytest.raises(OrderRejected, match="crypto is not bought on margin"):
        ex.submit(m.id, {"symbol": COIN, "side": "buy", "qty": "0.01", "type": "limit", "limit_price": "30000.00", "tif": "gtc"})
    assert ex.summary(m.id)["crypto_buying_power"] < D("400")


def test_raising_the_crypto_fee_is_refused_while_open_crypto_buys_could_not_pay_it():
    ex, feed, clock, m, c = make()
    for acct in (m, c):
        ex.update_settings(acct.id, {"crypto_fee_rate": "0"})
        [o] = ex.submit(acct.id, {"symbol": COIN, "side": "buy", "type": "limit", "qty": "2.5", "limit_price": "39999.00",
                                  "tif": "gtc"})
        with pytest.raises(ValueError, match=f"cannot be raised to 5%.*{o.id} \\(buy 2.5 SIMCOIN/USD\\)"):
            ex.update_settings(acct.id, {"crypto_fee_rate": "5"})
        assert ex.crypto_fee_rate(acct) == 0
        ex.update_settings(acct.id, {"crypto_fee_rate": "0.001"})  # a rise the cash still covers is fine
        ex.cancel(acct.id, o.id)
        ex.update_settings(acct.id, {"crypto_fee_rate": "5"})
    cl = TestClient(create_app(ex, run_loop=False))
    ex.update_settings(c.id, {"crypto_fee_rate": "0"})
    ex.submit(c.id, {"symbol": COIN, "side": "buy", "type": "limit", "qty": "2.5", "limit_price": "39999.00", "tif": "gtc"})
    r = cl.put(f"/api/accounts/{c.id}/settings", json={"crypto_fee_rate": "5"})
    assert r.status_code == 400 and "Cancel or reduce them first" in r.json()["detail"]
    r = cl.post(f"/api/accounts/{c.id}/settings/reset")
    assert r.status_code == 400  # the default fee (0.25%) is a rise these open buys cannot pay


def test_cash_account_needs_the_cash_and_the_fee():
    ex, feed, clock, m, c = make(cash=D("1000"))
    with pytest.raises(OrderRejected, match="insufficient cash"):
        ex.submit(c.id, {"symbol": COIN, "side": "buy", "qty": "0.025"})  # 1,000 + 2.50 fee
    ex.submit(c.id, {"symbol": COIN, "side": "buy", "qty": "0.0249"})
    assert c.cash >= 0


def test_day_orders_expire_at_the_utc_day_roll_not_the_stock_close():
    monday = datetime(2026, 11, 30, 20, 59, 0, tzinfo=timezone.utc).timestamp()  # 15:59 New York
    ex, feed, clock, m, c = make(t=monday)
    [o] = ex.submit(m.id, {"symbol": COIN, "side": "buy", "qty": "0.01", "type": "limit", "limit_price": "30000"})
    [g] = ex.submit(m.id, {"symbol": COIN, "side": "buy", "qty": "0.01", "type": "limit", "limit_price": "30000", "tif": "gtc"})
    [s] = ex.submit(m.id, {"symbol": "ABC", "side": "buy", "qty": "1", "type": "limit", "limit_price": "50"})
    move(ex, feed, clock, "40000.00", seconds=120)  # past the 16:00 close
    assert ex.session() != "regular" and s.status == "expired" and o.status == "working"
    clock.set(datetime(2026, 12, 1, 0, 0, 5, tzinfo=timezone.utc).timestamp())
    set_coin(feed, "40000.00")
    ex.tick()
    assert o.status == "expired" and g.status == "working"


def test_crypto_fills_continue_while_stocks_are_closed():
    ex, feed, clock, m, c = make()
    [o] = ex.submit(m.id, {"symbol": COIN, "side": "buy", "qty": "0.5", "type": "limit", "limit_price": "39000", "tif": "gtc"})
    move(ex, feed, clock, "38990.00")
    assert o.status == "filled" and o.avg_fill_price == D("38990.00")
    [st_] = ex.submit(m.id, {"symbol": COIN, "side": "sell", "qty": "0.5", "type": "stop", "stop_price": "38000"})
    move(ex, feed, clock, "37999.99")
    assert st_.status == "filled"


def test_market_fills_walk_the_book_in_price_increments():
    ex, feed, clock, m, c = make(price="0.004213", qinc="1", pinc="0.000001", mn="100")
    set_coin(feed, "0.004213", size="1000", step="0.000001")
    ex.tick()
    [o] = ex.submit(c.id, {"symbol": COIN, "side": "buy", "qty": "2500"})
    # 1000 at 0.004213, 1000 at 0.004214, 500 at 0.004215 -> 0.0042138 average, rounded up to the increment
    assert o.avg_fill_price == D("0.004214") and assets.on_step(o.avg_fill_price, D("0.000001"))
    assert c.cash == D("100000") - D("10.54") - D("0.03")


def test_pattern_day_trader_rule_ignores_crypto():
    ex, feed, clock, m, c = make(cash=D("10000"))
    for _ in range(5):
        ex.submit(m.id, {"symbol": COIN, "side": "buy", "qty": "0.01"})
        ex.submit(m.id, {"symbol": COIN, "side": "sell", "qty": "0.01"})
    assert ex.lifecycle.day_trade_count(m) == 0
    m.life.day_trades = [[ex.lifecycle.today().isoformat(), "ABC", f"O{i}"] for i in range(4)]
    assert ex.lifecycle.pdt_restricted(m)
    ex.submit(m.id, {"symbol": COIN, "side": "buy", "qty": "0.01"})  # crypto is not blocked by the rule


def test_fee_rate_is_an_account_setting():
    ex, feed, clock, m, c = make()
    from app.lifecycle import settings as cfg
    m.settings = cfg.update(m.settings, {"crypto_fee_rate": "1"})
    [o] = ex.submit(m.id, {"symbol": COIN, "side": "buy", "qty": "0.01"})
    assert o.fees == D("4.00")
    m.settings = cfg.update(m.settings, {"crypto_fee_rate": "0"})
    [o] = ex.submit(m.id, {"symbol": COIN, "side": "buy", "qty": "0.01"})
    assert o.fees == 0
    with pytest.raises(ValueError):
        cfg.update(m.settings, {"crypto_fee_rate": "6"})


def test_positions_orders_and_reports_show_crypto_with_its_precision(tmp_path):
    ex, feed, clock, m, c = make(qinc="0.00001")
    ex.submit(c.id, {"symbol": COIN, "side": "buy", "qty": "0.12345"})
    [row] = ex.positions(c.id)
    assert row["kind"] == row["asset_class"] == "crypto" and row["qty"] == D("0.12345") and row["multiplier"] == 1
    move(ex, feed, clock, "41000.00")
    ex.submit(c.id, {"symbol": COIN, "side": "sell", "qty": "0.10001"})
    rows = list(csv.DictReader(io.StringIO(fills_csv(c))))
    assert [r["qty"] for r in rows] == ["0.12345", "0.10001"] and rows[0]["fee"] == "12.35"
    ledger_rows = list(csv.DictReader(io.StringIO(ledger_csv(c))))
    by_txn = {}
    for r in ledger_rows:
        by_txn[r["transaction_id"]] = by_txn.get(r["transaction_id"], D(0)) + D(r["amount"])
    assert all(v == 0 for v in by_txn.values())
    from app.analytics.trades import closed_lots, gains_report
    [lot] = closed_lots(c)
    assert lot["asset"] == "crypto" and lot["qty"] == D("0.10001")
    assert gains_report(c)["rows"][0]["asset"] == "crypto"


# ---------------------------------------------------------------------------------------------
# Properties: ledger invariants with fractional quantities and percentage fees
# ---------------------------------------------------------------------------------------------
steps = st.lists(st.tuples(st.sampled_from(["buy", "sell"]), st.integers(min_value=1, max_value=40000),
                           st.integers(min_value=-300, max_value=300)), min_size=1, max_size=25)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps, st.sampled_from(["0", "0.1", "0.25", "1.5"]), st.sampled_from(["cash", "margin"]))
def test_ledger_balances_cash_never_negative_and_no_short(tmp_path_factory, seq, rate, kind):
    from app.lifecycle import settings as cfg
    ex, feed, clock, m, c = make(cash=D("5000"), qinc="0.00001", mn="0.00001")
    acct = m if kind == "margin" else c
    acct.settings = cfg.update(acct.settings, {"crypto_fee_rate": rate})
    price, paid, received, fees = D("40000.00"), D(0), D(0), D(0)
    for side, units, drift in seq:
        price = max(price + D(drift), D("100.00"))
        move(ex, feed, clock, str(price))
        qty = D(units) / D(100000)
        try:
            [o] = ex.submit(acct.id, {"symbol": COIN, "side": side, "qty": str(qty)})
        except OrderRejected as e:
            assert side == "sell" or "insufficient cash" in str(e) or "not bought on margin" in str(e)
            continue
        f = acct.fills[-1]
        notional = assets.round_to(f.qty * f.price, D("0.01"))
        paid += notional if side == "buy" else 0
        received += notional if side == "sell" else 0
        fees += f.fee
        assert acct.cash >= 0 and acct.position(COIN) >= 0
    assert all(sum(v for _, v in t.postings) == 0 for t in acct.ledger.transactions)
    assert acct.ledger.recompute() == acct.ledger.balances
    # Trade values are rounded to the cent per closed lot, so cash can differ from the whole-fill
    # rounding by at most a cent per lot piece; fees are exactly what the fills say.
    pieces = len(acct.fills) + len(acct.closed)
    assert abs(acct.cash - (D("5000") - paid + received - fees)) <= D("0.01") * pieces
    assert acct.ledger.balance(FEES) == fees and all(v == v.quantize(D("0.01")) for v in acct.ledger.balances.values())
    # Save, load: every quantity and price comes back exactly.
    d = tmp_path_factory.mktemp("save")
    ex.data_dir = str(d)
    ex.save(force=True)
    ex2 = Exchange(feed, clock, data_dir=str(d), session_mode="us")
    assert ex2.load()
    a2 = ex2.account(acct.id)
    assert a2.positions() == acct.positions() and a2.cash == acct.cash
    assert [(f.qty, f.price, f.fee) for f in a2.fills] == [(f.qty, f.price, f.fee) for f in acct.fills]
    rows = list(csv.DictReader(io.StringIO(fills_csv(a2))))
    assert [(D(r["qty"]), D(r["price"]), D(r["fee"])) for r in rows] == [(f.qty, f.price, f.fee) for f in acct.fills]


# ---------------------------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def api():
    feed = SyntheticFeed(seed=4, now=SAT, history_days=30, always_open=False)
    ex = Exchange(feed, SimClock(SAT), session_mode="us")
    ex.create_account("Margin", "margin", D("100000"))
    ex.create_account("Cash", "cash", D("25000"))
    ex.tick()
    return ex, TestClient(create_app(ex, run_loop=False))


def test_api_symbol_search_screener_and_market_data_for_crypto(api):
    ex, cl = api
    rows = cl.get("/api/symbols", params={"asset_class": "crypto"}).json()
    assert {r["symbol"] for r in rows} == set(CRYPTO_SYMBOLS) and all(r["asset_class"] == "crypto" for r in rows)
    assert all(r["asset_class"] == "stock" for r in cl.get("/api/symbols", params={"asset_class": "stock"}).json())
    assert cl.get("/api/symbols", params={"q": "simcoin"}).json()[0]["symbol"] == COIN
    scr = cl.get("/api/screener", params={"asset_class": "crypto"}).json()
    assert {r["symbol"] for r in scr} == set(CRYPTO_SYMBOLS)
    for path in ("bars/SIMCOIN/USD?tf=1d", "bars/SIMCOIN%2FUSD?tf=1m", "depth/SIMCOIN/USD", "trades/SIMCOIN/USD",
                 "fundamentals/SIMCOIN/USD"):
        r = cl.get(f"/api/{path}")
        assert r.status_code == 200, (path, r.text)
    f = cl.get("/api/fundamentals/SIMCOIN/USD").json()
    assert f["asset_class"] == "crypto" and f["qty_increment"] == "0.00001" and f["min_order_size"] == "0.0001"
    assert cl.get("/api/quotes", params={"symbols": "SIMDUST/USD"}).json()["SIMDUST/USD"]["last"] < 0.01


def test_api_orders_positions_watchlist_and_alerts_for_crypto(api):
    ex, cl = api
    aid = "A0002"
    r = cl.post(f"/api/accounts/{aid}/orders/preview", json={"symbol": COIN, "side": "buy", "notional": 500})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["asset_class"] == "crypto"
    r = cl.post(f"/api/accounts/{aid}/orders", json={"symbol": COIN, "side": "buy", "qty": "0.01234"})
    assert r.status_code == 200 and r.json()[0]["status"] == "filled" and r.json()[0]["asset_class"] == "crypto"
    pos = cl.get(f"/api/accounts/{aid}/positions").json()
    assert pos[0]["symbol"] == COIN and pos[0]["qty"] == 0.01234 and pos[0]["kind"] == "crypto"
    r = cl.post(f"/api/accounts/{aid}/orders", json={"symbol": COIN, "side": "sell", "qty": "1"})
    assert r.status_code == 422 and "crypto cannot be sold short" in r.json()["detail"]
    r = cl.post(f"/api/accounts/{aid}/orders", json={"symbol": COIN, "side": "buy", "qty": "0.01", "type": "market_on_close"})
    assert r.status_code == 422 and "24 hours a day" in r.json()["detail"]
    assert cl.put("/api/watchlists/Main", json=[COIN, "NVLX"]).json()["Main"] == [COIN, "NVLX"]
    al = cl.post("/api/alerts", json={"symbol": COIN, "op": "above", "price": "1"}).json()
    ex.clock.advance(1)
    ex.tick()
    assert any(a["id"] == al["id"] and a["triggered_at"] for a in cl.get("/api/alerts").json())
    s = cl.get(f"/api/accounts/{aid}").json()
    assert "crypto_buying_power" in s
    # Chart drawings are saved per symbol, crypto pairs included (the UI encodes the slash).
    assert cl.get("/api/chartstate/drawings/SIMCOIN%2FUSD").status_code == 200
    csv_text = cl.get(f"/api/accounts/{aid}/export/fills.csv")
    if csv_text.status_code == 200:
        assert "0.01234" in csv_text.text


# ---------------------------------------------------------------------------------------------
# Replay recording
# ---------------------------------------------------------------------------------------------
def test_replay_records_and_plays_back_crypto_exactly(tmp_path):
    from app.marketdata.replay.feed import ReplayFeed
    from app.marketdata.replay.recorder import record
    times = [SAT + i for i in range(60)]
    path = tmp_path / "coin.pdsession.gz"
    record(SyntheticFeed(seed=9, now=SAT, history_days=10), path, times, symbols=["SIMDUST/USD", COIN])
    live, rep = SyntheticFeed(seed=9, now=SAT, history_days=10), ReplayFeed.from_file(path)
    for t in times:
        live.step(t)
        rep.step(t)
        for s in ("SIMDUST/USD", COIN):
            a, b = live.quote(s), rep.quote(s)
            assert (a.bid, a.ask, a.bid_size, a.ask_size, a.last) == (b.bid, b.ask, b.bid_size, b.ask_size, b.last)
    assert rep.info[COIN]["type"] == "crypto" and rep.info[COIN]["qty_increment"] == "0.00001"
    d = rep.depth("SIMDUST/USD")
    assert all(x["size"] > 0 for x in d["bids"])
    ex = Exchange(rep, SimClock(times[-1]), session_mode="us")
    a = ex.create_account("R", "cash", D("1000"))
    [o] = ex.submit(a.id, {"symbol": "SIMDUST/USD", "side": "buy", "qty": "1000"})
    assert o.status == "filled"


# ---------------------------------------------------------------------------------------------
# Alpaca crypto adapter (documented example responses; no network)
# ---------------------------------------------------------------------------------------------
def fx(name):
    return (FIX / name).read_text(encoding="utf-8")


class Transport_(Transport):
    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def get(self, url, headers, timeout):
        parts = urllib.parse.urlsplit(url)
        params = dict(urllib.parse.parse_qsl(parts.query))
        self.requests.append({"url": url, "path": parts.path, "params": params, "headers": dict(headers)})
        r = self.routes.get(parts.path)
        if callable(r):
            r = r(params)
        if r is None:
            return 404, {}, json.dumps({"message": "not found"})
        return 200, {}, r


def crypto_routes():
    asset = json.loads(fx("asset_btc.json"))
    eth = dict(asset, symbol="ETH/USD", name="ETH/USD pair", min_order_size="0.001", min_trade_increment="0.001",
               price_increment="0.1")
    other = [dict(asset, symbol="ETH/BTC"), dict(asset, symbol="OLD/USD", status="inactive"),
             dict(asset, symbol="NOPE/USD", tradable=False)]
    return {"/v2/assets": lambda p: json.dumps([asset, eth] + other) if p.get("asset_class") == "crypto" else "[]",
            "/v1beta3/crypto/us/snapshots": fx("snapshots.json"), "/v1beta3/crypto/us/bars": fx("bars.json"),
            "/v1beta3/crypto/us/latest/orderbooks": fx("latest_orderbooks.json"),
            "/v1beta3/crypto/us/latest/quotes": fx("latest_quotes.json"),
            "/v1beta3/crypto/us/latest/trades": fx("latest_trades.json")}


def crypto_feed(t="2022-05-31T12:00:00Z"):
    from app.marketdata.alpaca_crypto import AlpacaCryptoFeed
    wall = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    tr = Transport_(crypto_routes())
    feed = AlpacaCryptoFeed("PKTEST", "SECRET", transport=tr, sleep=lambda s: None, wall_clock=lambda: wall)
    return feed, tr


def test_alpaca_crypto_asset_list_keeps_tradable_usd_pairs_with_increments():
    from app.marketdata.alpaca_crypto import assets_to_info
    info = assets_to_info([json.loads(fx("asset_btc.json"))])
    assert info["BTC/USD"]["qty_increment"] == "0.0001" and info["BTC/USD"]["price_increment"] == "1"
    assert info["BTC/USD"]["min_order_size"] == "0.0001" and info["BTC/USD"]["type"] == "crypto"
    feed, tr = crypto_feed()
    feed.load_assets()
    assert set(feed.crypto_info) == {"BTC/USD", "ETH/USD"} and set(feed.symbols()) >= {"BTC/USD", "ETH/USD"}
    req = [r for r in tr.requests if r["params"].get("asset_class") == "crypto"][0]
    assert req["headers"]["APCA-API-KEY-ID"] == "PKTEST" and "SECRET" not in req["url"]


def test_alpaca_crypto_snapshot_becomes_a_quote_with_coin_sizes():
    feed, tr = crypto_feed()
    feed.load_assets()
    feed.extra_interest = lambda: ["BTC/USD"]
    feed.poll_crypto()
    [q] = feed.step(0)
    assert (q.bid, q.ask, q.bid_size, q.ask_size) == (D("31741"), D("31742"), D("0.395"), D("0.395"))
    assert q.last == D("31744") and q.prev_close == D("31649") and q.volume == D("67.3518") and q.open == D("31660")
    req = [r for r in tr.requests if r["path"].endswith("/snapshots")][0]
    assert req["params"]["symbols"] == "BTC/USD"


def test_alpaca_crypto_stream_messages_update_quotes_and_prints():
    from app.marketdata.alpaca_crypto import CRYPTO_STREAM_URL
    feed, _ = crypto_feed()
    s = loads(fx("stream.json"))
    feed._on_crypto_stream([s["quote"], s["trade"], s["bar"]])
    out = {q.symbol: q for q in feed.step(0)}
    assert out["BAT/USD"].bid == D("0.35718") and out["BAT/USD"].ask_size == D("13561.902")
    assert out["AVAX/USD"].last == D("47.299") and out["AVAX/USD"].last_size == D("29.205707815")
    assert feed.trades("AVAX/USD")[0]["side"] == "sell"  # taker side S
    assert CRYPTO_STREAM_URL.format(loc="us") == "wss://stream.data.alpaca.markets/v1beta3/crypto/us"
    # The handshake is the stock stream's: connected -> auth -> authenticated -> subscribe.
    p = feed.crypto_protocol
    p.set_wanted(["BTC/USD"])
    out_msgs, _, _ = p.handle(s["connected"], 0)
    assert out_msgs == [{"action": "auth", "key": "PKTEST", "secret": "SECRET"}]
    out_msgs, _, _ = p.handle(s["authenticated"], 0)
    assert out_msgs == [{"action": "subscribe", "trades": ["BTC/USD"], "quotes": ["BTC/USD"]}]
    _, _, action = p.handle(s["error_406"], 0)
    assert action == "reconnect" and "connection" in p.error


def test_alpaca_crypto_bars_and_order_book():
    feed, tr = crypto_feed()
    feed.load_assets()
    bars = feed.bars("BTC/USD", "1m", 100)
    assert bars == [{"time": int(datetime(2022, 5, 27, 10, 18, tzinfo=timezone.utc).timestamp()), "open": 28999.0,
                     "high": 29003.0, "low": 28999.0, "close": 29003.0, "volume": 0.01}]
    req = [r for r in tr.requests if r["path"] == "/v1beta3/crypto/us/bars"][0]
    assert req["params"]["timeframe"] == "1Min" and req["params"]["sort"] == "desc" and req["params"]["symbols"] == "BTC/USD"
    d = feed.depth("BTC/USD")
    assert d["bids"] == [{"price": D("20846"), "size": D("0.1902")}] and d["asks"] == [{"price": D("20902"), "size": D("0.0097")}]
    assert d["simulated"] is False and d["source"] == "Alpaca"
    st_ = feed.status_info()["crypto"]
    assert st_["pairs"] == 2 and st_["location"] == "us"


def test_alpaca_crypto_orders_trade_against_provider_quotes():
    feed, _ = crypto_feed()
    feed.load_assets()
    feed.extra_interest = lambda: ["BTC/USD"]
    feed.poll_crypto()
    ex = Exchange(feed, SimClock(datetime(2022, 5, 28, 12, tzinfo=timezone.utc).timestamp()), session_mode="us")
    ex.tick()
    a = ex.create_account("Live", "cash", D("10000"))
    assert ex.quote_source("BTC/USD") == "alpaca"
    with pytest.raises(OrderRejected, match="multiple of 1"):
        ex.submit(a.id, {"symbol": "BTC/USD", "side": "buy", "qty": "0.01", "type": "limit", "limit_price": "31000.5"})
    [o] = ex.submit(a.id, {"symbol": "BTC/USD", "side": "buy", "qty": "0.0100"})
    assert o.status == "filled" and o.avg_fill_price == D("31742")


def test_crypto_adapter_has_no_order_endpoint():
    src = (Path(__file__).parent.parent / "app" / "marketdata" / "alpaca_crypto.py").read_text(encoding="utf-8")
    assert "/v2/orders" not in src and ".post(" not in src and "POST" not in src


# ---------------------------------------------------------------------------------------------
# Clock start setting (used by the weekend e2e server)
# ---------------------------------------------------------------------------------------------
def test_offset_clock_and_clock_start_setting(tmp_path, monkeypatch):
    from app.api.server import default_exchange
    from app.core.clock import OffsetClock, parse_start
    wall = [1000.0]
    c = OffsetClock(SAT, wall=lambda: wall[0])
    wall[0] += 5
    assert c.now() == SAT + 5
    assert parse_start("2026-11-28T15:00:00Z") == SAT == parse_start("2026-11-28T15:00:00")
    with pytest.raises(ValueError, match="2026-11-28T15:00:00Z"):
        parse_start("saturday")
    monkeypatch.setenv("PAPERDESK_DATA", str(tmp_path))
    monkeypatch.setenv("PAPERDESK_FEED", "synthetic")
    monkeypatch.setenv("PAPERDESK_SESSIONS", "us")
    monkeypatch.setenv("PAPERDESK_CLOCK_START", "2026-11-28T15:00:00Z")
    monkeypatch.setenv("PAPERDESK_ENV_FILE", "")
    ex = default_exchange()
    assert abs(ex.now() - SAT) < 60 and ex.session() == "closed" and ex.session_for(COIN) == "regular"


# ---------------------------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------------------------
def test_crypto_stop_buy_needs_a_limit():
    ex, feed, clock, m, c = make()
    for acct in (m, c):
        with pytest.raises(OrderRejected, match="a crypto stop order to buy needs a limit price"):
            ex.submit(acct.id, {"symbol": COIN, "side": "buy", "type": "stop", "qty": "2.4", "stop_price": "41000.00",
                                "tif": "gtc"})
    # A stop-limit buy is checked at its limit, so a gap above the stop cannot overspend.
    with pytest.raises(OrderRejected, match="insufficient cash"):
        ex.submit(c.id, {"symbol": COIN, "side": "buy", "type": "stop_limit", "qty": "2.4", "stop_price": "41000.00",
                         "limit_price": "60000.00", "tif": "gtc"})
    [o] = ex.submit(c.id, {"symbol": COIN, "side": "buy", "type": "stop_limit", "qty": "2.4", "stop_price": "41000.00",
                           "limit_price": "41500.00", "tif": "gtc"})
    move(ex, feed, clock, "60000.00")
    assert o.status == "working" and c.cash == D("100000")  # triggered, but the market is above the limit


@pytest.mark.parametrize("kind", ["cash", "margin"])
def test_a_crypto_buy_fill_is_capped_to_the_cash_at_fill_time(kind):
    ex, feed, clock, m, c = make()
    acct = m if kind == "margin" else c
    [o] = ex.submit(acct.id, {"symbol": COIN, "side": "buy", "type": "limit", "qty": "2.49", "limit_price": "39999.00",
                              "tif": "gtc"})
    from app.lifecycle import settings as cfg
    acct.settings = cfg.merged({**acct.settings, "crypto_fee_rate": "5"})  # bypasses the settings check on purpose
    move(ex, feed, clock, "39999.00")
    assert acct.cash >= 0 and o.status == "canceled" and "never bought with borrowed money" in o.reason
    assert D(0) < o.filled_qty < D("2.49") and o.filled_qty == acct.position(COIN)
    assert assets.on_step(o.filled_qty, D("0.0001")) and acct.fills[-1].qty == o.filled_qty
    assert not acct.margin_call and ex.summary(acct.id)["margin_call"] is False


actions = st.lists(st.tuples(
    st.sampled_from(["market", "limit", "stop_limit", "stop", "notional_market", "notional_limit"]),
    st.sampled_from(["buy", "sell"]), st.integers(min_value=1, max_value=300000),
    st.integers(min_value=-40, max_value=40), st.integers(min_value=-35, max_value=35)), min_size=1, max_size=20)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(actions, st.sampled_from(["0", "0.25", "2"]), st.sampled_from(["cash", "margin"]))
def test_price_gaps_never_make_cash_negative_or_a_short(seq, rate, kind):
    """Orders of every allowed type rest while the price gaps by up to 35% between entry and fill."""
    from app.lifecycle import settings as cfg
    ex, feed, clock, m, c = make(cash=D("20000"), qinc="0.00001", mn="0.00001")
    acct = m if kind == "margin" else c
    acct.settings = cfg.update(acct.settings, {"crypto_fee_rate": rate})
    price = D("40000.00")
    for typ, side, units, off_pct, gap_pct in seq:
        ref = (price * (100 + off_pct) / 100).quantize(D("0.01"))
        req = {"symbol": COIN, "side": side, "tif": "gtc"}
        frac = D(units % 10 + 90) / 100 if units % 2 else D(units % 100 + 1) / 100  # often nearly all the cash
        budget = acct.cash * frac if side == "buy" else acct.position(COIN) * ref * frac
        if typ.startswith("notional"):
            req.update(notional=str(max(budget, D(1)).quantize(D("0.01"))), type=typ.split("_")[1])
        else:
            req.update(qty=str(max(assets.floor_to(budget / ref, D("0.00001")), D("0.00001"))), type=typ)
        if req["type"] in ("limit", "stop_limit"):
            req["limit_price"] = str(ref)
        if req["type"] in ("stop", "stop_limit"):
            req["stop_price"] = str(ref)
        if req["type"] == "market":
            req["tif"] = "day"
        try:
            ex.submit(acct.id, req)
        except OrderRejected:
            pass
        if gap_pct % 5 == 0:  # the fee rate is raised under resting orders (bypassing the settings check)
            acct.settings = cfg.merged({**acct.settings, "crypto_fee_rate": "5"})
        move(ex, feed, clock, str(ref))  # first trade at the order's own level (fills limits at the limit) ...
        price = max((ref * (100 + gap_pct) / 100).quantize(D("0.01")), D("1.00"))
        move(ex, feed, clock, str(price))  # ... then gap past it
        assert acct.cash >= 0 and acct.position(COIN) >= 0, (acct.cash, acct.position(COIN))
    assert all(sum(v for _, v in t.postings) == 0 for t in acct.ledger.transactions)
    assert acct.ledger.recompute() == acct.ledger.balances


def test_simulated_research_ignores_the_coins():
    from app.marketdata.simresearch import for_replay
    feed = SyntheticFeed(seed=3, now=SAT, history_days=10)
    r = feed.research
    assert not any(assets.is_crypto(s) for s in r.stocks())
    assert r.upcoming_releases(COIN, SAT - 400 * 86400, SAT + 400 * 86400) == []
    assert for_replay(feed) is not None  # a replay of synthetic data with coins still gets research
