from datetime import datetime

import pytest

from app.core import calendar as cal
from app.core.clock import SimClock
from app.exchange import Exchange, OrderRejected
from app.marketdata.feed import SimFeed
from app.options import chain
from app.options.symbology import build
from helpers import D, T0, make_exchange, move


def acct(ex, type="margin", cash="100000"):
    return ex.create_account("t", type, D(cash)).id


def test_market_buy_updates_account():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    [o] = ex.submit(a, {"symbol": "abc", "side": "buy", "qty": 100})
    assert o.status == "filled"
    s = ex.summary(a)
    assert s["cash"] == D("90000") and s["equity"] == D("100000") and s["buying_power"] == D("190000.00")
    move(ex, feed, clock, "ABC", "110")
    s = ex.summary(a)
    assert s["equity"] == D("101000") and s["unrealized_pnl"] == D("1000") and s["day_pnl"] == D("1000")
    [p] = ex.positions(a)
    assert p["qty"] == 100 and p["avg_cost"] == D("100") and p["unrealized_pnl"] == D("1000")


def test_buying_power_limits():
    ex, _, _ = make_exchange()
    a = acct(ex)
    with pytest.raises(OrderRejected, match="buying power"):
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 2100})
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1000})
    # a resting buy reserves buying power, so a second large order is refused
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 900, "type": "limit", "limit_price": "99"})
    with pytest.raises(OrderRejected):
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 900, "type": "limit", "limit_price": "99"})
    assert ex.summary(a)["buying_power"] < D("12000")


def test_cash_account_rules():
    ex, _, _ = make_exchange()
    a = acct(ex, "cash", "10000")
    with pytest.raises(OrderRejected, match="margin account"):
        ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 10})
    with pytest.raises(OrderRejected, match="insufficient cash"):
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 101})
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
    assert ex.summary(a)["cash"] == 0 and ex.summary(a)["buying_power"] == 0
    with pytest.raises(OrderRejected):  # cannot sell more than held
        ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 101})
    assert ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 100})[0].status == "filled"


def test_short_sale_and_cover():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 100})
    assert ex.positions(a)[0]["qty"] == -100
    move(ex, feed, clock, "ABC", "90")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
    # sold at the 99.99 bid, covered at the 90.00 ask
    assert ex.summary(a)["realized_pnl"] == D("999.00") and not ex.positions(a)


def test_bracket_end_to_end():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    entry, tp, sl = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100, "take_profit": "105", "stop_loss": "97"})
    assert (entry.status, tp.status, sl.status) == ("filled", "working", "working")
    move(ex, feed, clock, "ABC", "105.20")
    assert tp.status == "filled" and sl.status == "canceled"
    assert ex.summary(a)["realized_pnl"] == D("519.00") and not ex.positions(a)  # sold at the 105.19 bid
    with pytest.raises(OrderRejected, match="take profit"):
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100, "take_profit": "90"})


def test_closing_is_allowed_when_under_requirement():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1900})
    move(ex, feed, clock, "ABC", "60")
    s = ex.summary(a)
    assert s["margin_call"] and s["buying_power"] == 0
    with pytest.raises(OrderRejected):
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1})
    assert ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 1900})[0].status == "filled"


def test_validation_messages():
    ex, _, _ = make_exchange()
    a = acct(ex)
    bad = [({"symbol": "NOPE", "side": "buy", "qty": 1}, "unknown symbol"),
           ({"symbol": "ABC", "side": "buy", "qty": 0}, "quantity"),
           ({"symbol": "ABC", "side": "buy", "qty": "abc"}, "invalid number"),
           ({"symbol": "ABC", "side": "hold", "qty": 1}, "side"),
           ({"symbol": "ABC", "side": "buy", "qty": 1, "type": "limit"}, "limit price is required"),
           ({"symbol": "ABC", "side": "buy", "qty": 1, "type": "limit", "limit_price": "10.001"}, "decimal"),
           ({"symbol": "ABC", "side": "buy", "qty": 1, "type": "stop"}, "stop price is required"),
           ({"symbol": "ABC", "side": "buy", "qty": 1, "type": "trailing_stop"}, "exactly one"),
           ({"symbol": "ABC", "side": "buy", "qty": "1.5", "type": "stop", "stop_price": "120"}, "fractional"),
           ({"symbol": "ABC", "side": "buy", "qty": 1, "limit_price": float("nan"), "type": "limit"}, "invalid number")]
    for req, msg in bad:
        with pytest.raises(OrderRejected, match=msg):
            ex.submit(a, req)
    assert ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": "0.5"})[0].status == "filled"


def test_idempotent_client_id_and_cancel_replace():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    req = {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "limit_price": "95", "client_id": "k1"}
    [o1] = ex.submit(a, req)
    [o2] = ex.submit(a, req)
    assert o1.id == o2.id and len(ex.orders(a)) == 1
    ex.replace(a, o1.id, {"limit_price": "96", "qty": 20})
    assert o1.limit_price == D("96") and o1.qty == 20
    with pytest.raises(OrderRejected):
        ex.replace(a, o1.id, {"qty": 100000})
    assert o1.qty == 20
    ex.cancel(a, o1.id)
    assert o1.status == "canceled"
    with pytest.raises(ValueError):
        ex.cancel(a, o1.id)
    with pytest.raises(KeyError):
        ex.cancel(acct(ex), o1.id)  # another account cannot touch it


def test_day_orders_expire_and_gtc_survive():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    [day] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "limit_price": "90"})
    [gtc] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "limit_price": "90", "tif": "gtc"})
    move(ex, feed, clock, "ABC", "100", seconds=86400)
    assert day.status == "expired" and gtc.status == "working"


def _option(kind, strike, now=T0):
    exp = chain.expirations(cal.ny_date(now))[0]
    return build("ABC", exp, kind, D(strike)), cal.close_ts(exp)


def test_long_call_exercised_at_expiry():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    sym, expiry_ts = _option("C", "100")
    [o] = ex.submit(a, {"symbol": sym, "side": "buy", "qty": 2})
    assert o.status == "filled" and o.fees == D("1.30")
    paid = D("100000") - ex.summary(a)["cash"]
    assert paid == o.avg_fill_price * 200 + D("1.30")
    clock.set(expiry_ts - 5)
    move(ex, feed, clock, "ABC", "120", seconds=10)  # now past the close, 20 in the money
    pos = {p["symbol"]: p for p in ex.positions(a)}
    assert set(pos) == {"ABC"} and pos["ABC"]["qty"] == 200 and pos["ABC"]["avg_cost"] == D("100")
    assert ex.summary(a)["cash"] == D("100000") - paid - D("20000")
    assert [f.kind for f in ex.account(a).fills[-2:]] == ["exercise", "exercise"]
    with pytest.raises(OrderRejected, match="expired"):
        ex.submit(a, {"symbol": sym, "side": "buy", "qty": 1})


def test_out_of_the_money_option_expires_worthless_and_short_put_is_assigned():
    ex, feed, clock = make_exchange()
    a = acct(ex)
    call, expiry_ts = _option("C", "110")
    put, _ = _option("P", "105")
    ex.submit(a, {"symbol": call, "side": "buy", "qty": 1})
    ex.submit(a, {"symbol": put, "side": "sell", "qty": 1})
    [resting] = ex.submit(a, {"symbol": call, "side": "sell", "qty": 1, "type": "limit", "limit_price": "50", "tif": "gtc"})
    cash_before = ex.summary(a)["cash"]
    clock.set(expiry_ts - 5)
    move(ex, feed, clock, "ABC", "101", seconds=10)
    pos = {p["symbol"]: p["qty"] for p in ex.positions(a)}
    assert pos == {"ABC": 100}  # assigned on the put at 105; the call expired worthless
    assert ex.summary(a)["cash"] == cash_before - D("10500")
    assert resting.status == "expired"
    kinds = [f.kind for f in ex.account(a).fills[-3:]]
    assert sorted(kinds) == ["assignment", "assignment", "expiration"]


def test_option_orders_fill_from_model_quote_and_respect_levels():
    ex, feed, clock = make_exchange()
    cash = acct(ex, "cash", "5000")
    call, _ = _option("C", "100")
    with pytest.raises(OrderRejected, match="uncovered"):
        ex.submit(cash, {"symbol": call, "side": "sell", "qty": 1})
    with pytest.raises(OrderRejected, match="whole contracts"):
        ex.submit(cash, {"symbol": call, "side": "buy", "qty": "0.5"})
    q = ex.quote(call)
    [o] = ex.submit(cash, {"symbol": call, "side": "buy", "qty": 1, "type": "limit", "limit_price": str(q.bid)})
    assert o.status == "working"
    move(ex, feed, clock, "ABC", "90")  # the call gets cheaper; its ask falls through our bid
    assert o.status == "filled" and o.avg_fill_price <= q.bid


def test_alerts_fire_once():
    ex, feed, clock = make_exchange()
    ex.add_alert("ABC", "above", "105")
    move(ex, feed, clock, "ABC", "104")
    assert not [e for e in ex.drain_events() if e["type"] == "alert"]
    move(ex, feed, clock, "ABC", "106")
    move(ex, feed, clock, "ABC", "107")
    assert len([e for e in ex.drain_events() if e["type"] == "alert"]) == 1


def test_state_survives_restart(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
    [resting] = ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 100, "type": "limit", "limit_price": "108", "tif": "gtc"})
    ex.watchlists["Main"] = ["ABC"]
    before = ex.summary(a)
    ex.save(force=True)

    feed2, clock2 = SimFeed(), SimClock(clock.now())
    ex2 = Exchange(feed2, clock2, data_dir=str(tmp_path))
    assert ex2.load()
    feed2.set_quote("ABC", "99.99", "100", "100")
    ex2.tick()
    assert ex2.summary(a) == before and ex2.watchlists == {"Main": ["ABC"]}
    move(ex2, feed2, clock2, "ABC", "108.50")
    assert ex2.engine.orders[resting.id].status == "filled" and not ex2.positions(a)
    [new] = ex2.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1})
    assert new.id not in (resting.id,) and len({f.id for f in ex2.account(a).fills}) == len(ex2.account(a).fills)


def test_us_session_mode_queues_market_orders_until_the_open():
    sunday = datetime(2026, 11, 22, 12, 0, tzinfo=cal.NY).timestamp()
    clock, feed = SimClock(sunday), SimFeed()
    ex = Exchange(feed, clock, session_mode="us")
    feed.set_quote("ABC", "99.99", "100", "100")
    ex.tick()
    a = acct(ex)
    [o] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    assert o.status == "working" and ex.session() == "closed"
    clock.set(datetime(2026, 11, 23, 9, 30, 1, tzinfo=cal.NY).timestamp())
    feed.set_quote("ABC", "101.99", "102", "102")
    ex.tick()
    assert o.status == "filled" and o.avg_fill_price == D("102")
    clock.set(datetime(2026, 11, 23, 16, 0, 1, tzinfo=cal.NY).timestamp())
    [d] = ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 10, "type": "limit", "limit_price": "150"})
    clock.set(datetime(2026, 11, 23, 20, 0, 1, tzinfo=cal.NY).timestamp())
    ex.tick()
    assert d.status == "expired"


def test_reset_account():
    ex, _, _ = make_exchange()
    a = acct(ex)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
    ex.reset_account(a, D("5000"))
    s = ex.summary(a)
    assert s["cash"] == D("5000") and not ex.positions(a) and not ex.orders(a)
