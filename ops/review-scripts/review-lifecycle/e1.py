import sys
sys.path.insert(0, "/home/user/wt/review-lifecycle/backend/tests"); sys.path.insert(0, "/home/user/wt/review-lifecycle/backend")
from datetime import date, datetime, timedelta, timezone
from helpers import D, T0, make_exchange, move
from app.exchange import Exchange, OrderRejected
from app.lifecycle.actions import dividend, split
from app.core.clock import SimClock
from app.marketdata.feed import SimFeed

def deep(ex, feed, clock, price, seconds=1, sym="ABC"):
    move(ex, feed, clock, sym, price, seconds=seconds, bid_size=100000, ask_size=100000)
def sec(t): print("\n=== " + t)

sec("1 replace() bypasses settled-cash rule")
ex, feed, clock = make_exchange(); deep(ex, feed, clock, "100")
a = ex.create_account("c", "cash", D("10000")).id
ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 100})
s = ex.summary(a); print("cash", s["cash"], "settled", s["settled_cash"], "bp", s["buying_power"])
try:
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 50}); print("direct buy accepted (BUG)")
except OrderRejected as e: print("direct buy rejected:", str(e)[:70])
# tiny resting order, then replace
try:
    [o] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "limit", "limit_price": "0.01"})
    print("1 share @0.01 accepted", o.status)
except OrderRejected as e:
    print("tiny order rejected:", e); o = None
if o:
    ex.replace(a, o.id, {"qty": 90, "limit_price": "100.00"})
    deep(ex, feed, clock, "100")
    print("position after replace:", ex.account(a).position("ABC"), "cash", ex.account(a).cash, "unsettled", ex.account(a).life.unsettled_total())

sec("1b zero settled cash; any order while unsettled? buy 1 share at 0.01 requires .01 settled")
sec("2 PDT blocks close in a margin call")
ex, feed, clock = make_exchange(); deep(ex, feed, clock, "100")
a = ex.create_account("m", "margin", D("20000")).id
for i in range(3):
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10}); ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 10})
print("day trades", ex.summary(a)["day_trades"])
ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 390})
deep(ex, feed, clock, "60")
s = ex.summary(a); print("equity", s["equity"], "maint", s["maintenance_requirement"], "margin_call", s["margin_call"])
try:
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 390}); print("close accepted")
except OrderRejected as e: print("CLOSE REJECTED:", e)
# bracket bypass
ex, feed, clock = make_exchange(); deep(ex, feed, clock, "100")
a = ex.create_account("m", "margin", D("20000")).id
for i in range(3):
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10}); ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 10})
ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10, "take_profit": "101", "stop_loss": "99"})
deep(ex, feed, clock, "102")
print("bracket: pos", ex.account(a).position("ABC"), "day trades", ex.summary(a)["day_trades"])

sec("3 missing quote -> margin call + liquidation of other positions")
ex, feed, clock = make_exchange(ABC="100", XYZ="100"); deep(ex, feed, clock, "100"); deep(ex, feed, clock, "100", sym="XYZ")
a = ex.create_account("m", "margin", D("100000")).id
ex.account(a).settings["margin_call_grace_hours"] = "0"
ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 900}); ex.submit(a, {"symbol": "XYZ", "side": "buy", "qty": 900})
print("eq", ex.summary(a)["equity"], "maint", ex.summary(a)["maintenance_requirement"])
del feed.quotes["XYZ"]
clock.advance(1); ex.tick(); clock.advance(1); ex.tick()
print("after quote loss: ABC", ex.account(a).position("ABC"), "XYZ", ex.account(a).position("XYZ"), "since", ex.account(a).life.margin_call_since)
print([t.memo[:110] for t in ex.account(a).ledger.transactions if t.kind in ("margin_call","liquidation")])

sec("6 alerts after split")
ex, feed, clock = make_exchange(); deep(ex, feed, clock, "100")
ex.add_alert("ABC", "below", "80")
day = date(1970,1,1)+timedelta(days=int(clock.now()//86400)+1)
feed.schedule(split("ABC", day, 2, 1))
clock.advance(86400); ex.tick()
feed.set_quote("ABC", D("49.99"), D("50"), D("50")); ex.tick()
print("alert:", ex.alerts)

sec("7 margin call flapping -> ledger spam")
ex, feed, clock = make_exchange(); deep(ex, feed, clock, "100")
a = ex.create_account("m", "margin", D("100000")).id
ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1900})
n0 = len(ex.account(a).ledger.transactions)
for i in range(600):
    deep(ex, feed, clock, "63.15" if i % 2 else "63.17")
print("ledger txns added in 600 ticks:", len(ex.account(a).ledger.transactions) - n0, "since", ex.account(a).life.margin_call_since)
