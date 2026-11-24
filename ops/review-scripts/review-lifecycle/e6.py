import sys, random, tempfile, json, os
sys.path.insert(0, "/home/user/wt/review-lifecycle/backend/tests"); sys.path.insert(0, "/home/user/wt/review-lifecycle/backend")
from datetime import date, datetime, timedelta, timezone
from helpers import D, make_exchange, move
from app.exchange import Exchange
from app.core.clock import SimClock
from app.options import chain
from app.options.symbology import build
from app.marketdata.synthetic import SyntheticFeed
def ts(y, mo, d, h=15, mi=0, s=0): return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()
def deep(ex, feed, clock, price, sym="ABC"): move(ex, feed, clock, sym, price, bid_size=1000000, ask_size=1000000)
print("=== naked call created by liquidation")
ex, feed, clock = make_exchange(); deep(ex, feed, clock, "100")
a = ex.create_account("m", "margin", D("80000")); a.settings["margin_call_grace_hours"]="0"
exps = chain.expirations(date(2026,11,24)); c = build("ABC", exps[3], "C", D("110"))
ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 1500}); ex.submit(a.id, {"symbol": c, "side": "sell", "qty": 2})
deep(ex, feed, clock, "48")
s = ex.summary(a.id); print("before", {k:str(v) for k,v in a.positions().items()}, "eq", s["equity"], "maint", s["maintenance_requirement"])
deep(ex, feed, clock, "48")
s = ex.summary(a.id); print("after ", {k:str(v) for k,v in a.positions().items()}, "eq", s["equity"], "maint", s["maintenance_requirement"])
print([t.memo for t in a.ledger.transactions if t.kind=="liquidation"])

print("=== restart on split date with synthetic feed (double adjust?) and on the 1st of the month")
d = tempfile.mkdtemp()
t0 = ts(2027, 9, 14, 23, 59, 50)
feed = SyntheticFeed(seed=7, now=t0, always_open=True); clock = SimClock(t0)
ex = Exchange(feed, clock, data_dir=d); ex.tick()
a = ex.create_account("m", "margin", D("100000")).id
ex.submit(a, {"symbol": "RAIL", "side": "buy", "qty": 600}); ex.save(force=True)
print("pre  last", feed.quote("RAIL").last, "pos", ex.account(a).position("RAIL"), "eq", ex.summary(a)["equity"])
clock.advance(15); ex.tick(); ex.save()
print("post last", feed.quote("RAIL").last, "pos", ex.account(a).position("RAIL"), "eq", ex.summary(a)["equity"], "hist close", feed.store.daily["RAIL"][-2][4])
for i in range(2):
    feed2 = SyntheticFeed(seed=7, now=clock.now(), always_open=True); ex2 = Exchange(feed2, clock, data_dir=d); ex2.load()
    clock.advance(5); ex2.tick(); ex2.save()
    print("restart", i, "last", feed2.quote("RAIL").last, "pos", ex2.account(a).position("RAIL"), "eq", ex2.summary(a)["equity"], "hist", feed2.store.daily["RAIL"][-2][4], "applied", ex2.lifecycle.applied)
# crash window: state saved, market not
st = json.load(open(os.path.join(d, "state.json")))
# month-end across restart
clock.set(ts(2027,10,1,0,0,5)); ex2.tick(); ex2.save()
feed3 = SyntheticFeed(seed=7, now=clock.now(), always_open=True); ex3 = Exchange(feed3, clock, data_dir=d); ex3.load(); clock.advance(5); ex3.tick()
print("interest postings:", [(t.memo, str(t.postings[0][1])) for t in ex3.account(a).ledger.transactions if t.kind=="interest"])
print("=== market.json lost / rejected after a split")
os.remove(os.path.join(d, "market.json"))
feed4 = SyntheticFeed(seed=7, now=clock.now(), always_open=True); ex4 = Exchange(feed4, clock, data_dir=d); ex4.load(); clock.advance(5); ex4.tick()
print("last", feed4.quote("RAIL").last, "pos", ex4.account(a).position("RAIL"), "eq", ex4.summary(a)["equity"], "feed applied", feed4.applied_actions)
