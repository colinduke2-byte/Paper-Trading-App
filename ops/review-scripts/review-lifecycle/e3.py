import sys, random
sys.path.insert(0, "/home/user/wt/review-lifecycle/backend/tests"); sys.path.insert(0, "/home/user/wt/review-lifecycle/backend")
from datetime import date, datetime, timedelta, timezone
from helpers import D
from app.exchange import Exchange
from app.core.clock import SimClock
from app.marketdata.synthetic import SyntheticFeed
def ts(y, mo, d, h=15): return datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp()
def run(daily):
    t0 = ts(2027, 9, 1)
    feed = SyntheticFeed(seed=7, now=t0, always_open=True); clock = SimClock(t0)
    ex = Exchange(feed, clock); ex.tick()
    a = ex.create_account("m", "margin", D("1000000")).id
    ex.submit(a, {"symbol": "RAIL", "side": "buy", "qty": 100})
    if daily:
        while clock.now() < ts(2027,12,5):
            clock.advance(86400); ex.tick()
    else:
        clock.set(ts(2027, 12, 5)); ex.tick()
    acc = ex.account(a)
    print("daily" if daily else "offline", "pos", acc.position("RAIL"), "last", feed.quote("RAIL").last, [(t.memo, [str(v) for k,v in t.postings if k=="cash"]) for t in acc.ledger.transactions if t.kind=="dividend"])
run(True); run(False)
