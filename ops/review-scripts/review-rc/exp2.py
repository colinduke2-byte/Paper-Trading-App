import sys, json, tempfile, os
sys.path.insert(0, "/home/user/wt/review-rc/backend"); sys.path.insert(0, "/home/user/wt/review-rc/backend/tests")
from datetime import date, timedelta
from decimal import Decimal as Dc
from app.core import calendar as cal
from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata.feed import SimFeed
from app.options import chain
from app.options.symbology import build, parse
from helpers import T0, make_exchange, move

class NoSnapFeed(SimFeed):
    """Like the Alpaca feed: symbol list known, no quotes until the provider delivers, no market.json."""
    name = "alpaca-like"; simulated = False

print("=== 2: restart in a live-data-like feed (no persisted quotes) with options that expired while down")
tmp = tempfile.mkdtemp()
clock = SimClock(T0); feed = NoSnapFeed()
ex = Exchange(feed, clock, data_dir=tmp)
feed.set_quote("ABC", "99.99", "100.00", "100.00"); ex.tick()
acc = ex.create_account("t", "margin", Dc("100000")); a = acc.id
exp = chain.expirations(cal.ny_date(T0))[0]
put = build("ABC", exp, "P", Dc("90")); call = build("ABC", exp, "C", Dc("90"))
ex.submit(a, {"symbol": put, "side": "buy", "qty": 1})     # far OTM long put
ex.submit(a, {"symbol": call, "side": "buy", "qty": 1})    # ITM long call (worth ~10)
ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
print("before: positions", acc.positions(), "equity", ex.summary(a)["equity"])
ex.save(force=True)
# restart after expiry; the feed knows the symbols but has not delivered a quote yet
clock2 = SimClock(parse(put).expiry_ts + 3600); feed2 = NoSnapFeed({"ABC": {"symbol": "ABC", "name": "ABC", "sector": "", "vol": 0.3}})
ex2 = Exchange(feed2, clock2, data_dir=tmp); print("loaded", ex2.load())
acc2 = ex2.account(a)
try:
    print("positions() before first quote:", [r["symbol"] for r in ex2.positions(a)])
except Exception as e:
    print("positions() before first quote RAISES:", repr(e))
ex2.tick()   # first tick: no quote yet
print("fills booked on first tick:", [(f.kind, f.symbol[-9:], f.side, f.qty, f.price) for f in acc2.fills[3:]])
print("equity_history tail:", acc2.equity_history[-2:])
feed2.set_quote("ABC", "99.99", "100.00", "100.00"); clock2.advance(5); ex2.tick()
print("after quote arrives: positions", acc2.positions(), "cash", acc2.cash, "equity", ex2.summary(a)["equity"])

print("=== 3: day order across a restart ('always' mode and 'us' mode)")
for mode in ("always", "us"):
    tmp = tempfile.mkdtemp()
    # Monday 2026-11-23 15:00 UTC = 10:00 ET regular session
    t0 = 1795500000.0 + 3*3600 + 40*60
    clock = SimClock(t0); feed = SimFeed(); ex = Exchange(feed, clock, data_dir=tmp, session_mode=mode)
    feed.set_quote("ABC", "99.99", "100.00", "100.00"); ex.tick()
    a = ex.create_account("t", "margin", Dc("100000")).id
    [o] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "limit_price": "90", "tif": "day"})
    ex.save(force=True)
    clock2 = SimClock(t0 + 2*86400); feed2 = SimFeed(); ex2 = Exchange(feed2, clock2, data_dir=tmp, session_mode=mode); ex2.load()
    feed2.set_quote("ABC", "99.99", "100.00", "100.00"); ex2.tick()
    feed2.set_quote("ABC", "89.00", "89.01", "89.01"); clock2.advance(1); ex2.tick()
    o2 = ex2.engine.orders[o.id]
    print(mode, "session now", ex2.session(), "-> day order placed two days earlier:", o2.status, "filled", o2.filled_qty, "day_start_equity", ex2.account(a).day_start_equity)
