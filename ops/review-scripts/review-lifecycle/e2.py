import sys, random
sys.path.insert(0, "/home/user/wt/review-lifecycle/backend/tests"); sys.path.insert(0, "/home/user/wt/review-lifecycle/backend")
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from helpers import D, T0, make_exchange, move
from app.exchange import Exchange, OrderRejected
from app.lifecycle.actions import dividend, split
from app.lifecycle import settings as cfg
from app.core.clock import SimClock
from app.marketdata.feed import SimFeed
from app.marketdata.synthetic import SyntheticFeed
def deep(ex, feed, clock, price, seconds=1, sym="ABC"):
    move(ex, feed, clock, sym, price, seconds=seconds, bid_size=100000, ask_size=100000)
def sec(t): print("\n=== " + t)
def ts(y, mo, d, h=15): return datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp()

sec("1 replace() bypasses settled-cash rule")
ex, feed, clock = make_exchange(); deep(ex, feed, clock, "100")
a = ex.create_account("c", "cash", D("10000")).id
ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 50}); ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 50})
s = ex.summary(a); print("cash", s["cash"], "settled", s["settled_cash"], "bp", s["buying_power"])
try: ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 90}); print("direct accepted")
except OrderRejected as e: print("direct buy 90 rejected")
[o] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "limit", "limit_price": "0.01"})
ex.replace(a, o.id, {"qty": 99, "limit_price": "100.00"})
deep(ex, feed, clock, "100")
acc = ex.account(a); print("after replace: pos", acc.position("ABC"), "cash", acc.cash, "unsettled", acc.life.unsettled_total(), "settled", acc.cash-acc.life.unsettled_total())

sec("4 catch-up across split then dividend (synthetic, 2027: RAIL splits Sep 15, div Nov)")
t0 = ts(2027, 9, 1)
feed = SyntheticFeed(seed=7, now=t0, always_open=True); clock = SimClock(t0)
ex = Exchange(feed, clock); ex.tick()
acts = [x for x in feed.corporate_actions(date(2027,9,1), date(2027,12,1)) if x.symbol=="RAIL"]
print([ (x.id, str(x.amount), x.new) for x in acts])
a = ex.create_account("m", "margin", D("1000000")).id
ex.submit(a, {"symbol": "RAIL", "side": "buy", "qty": 100})
# variant A: online every day
import copy
clockA = clock
# offline jump
clock.set(ts(2027, 12, 1)); ex.tick()
acc = ex.account(a)
print("offline: pos", acc.position("RAIL"), [ (t.kind, t.memo, [str(v) for k,v in t.postings if k=="cash"]) for t in acc.ledger.transactions if t.kind=="dividend"], acc.life.pending_dividends)
# online daily
feed = SyntheticFeed(seed=7, now=t0, always_open=True); clock = SimClock(t0)
ex = Exchange(feed, clock); ex.tick()
a = ex.create_account("m", "margin", D("1000000")).id
ex.submit(a, {"symbol": "RAIL", "side": "buy", "qty": 100})
while clock.now() < ts(2027,12,1):
    clock.advance(86400); ex.tick()
acc = ex.account(a)
print("daily:   pos", acc.position("RAIL"), [ (t.memo, [str(v) for k,v in t.postings if k=="cash"]) for t in acc.ledger.transactions if t.kind=="dividend"])

sec("5 dividend drop vs stop order + equity dip (synthetic)")
t0 = ts(2027, 11, 1)
feed = SyntheticFeed(seed=7, now=t0, always_open=True); clock = SimClock(t0)
ex = Exchange(feed, clock); ex.tick()
nxt = sorted([x for x in feed.corporate_actions(date(2027,11,2), date(2027,12,30)) if x.kind=="dividend"], key=lambda x: -x.amount)[0]
print("div:", nxt.id, nxt.amount, nxt.pay_date)
sym = nxt.symbol
clock.set(datetime(nxt.ex_date.year, nxt.ex_date.month, nxt.ex_date.day, tzinfo=timezone.utc).timestamp() - 5); ex.tick()
a = ex.create_account("m", "margin", D("1000000")).id
ex.submit(a, {"symbol": sym, "side": "buy", "qty": 1000})
last = feed.quote(sym).last
[st] = ex.submit(a, {"symbol": sym, "side": "sell", "qty": 1000, "type": "stop", "stop_price": str(last - D("0.60")), "tif": "gtc"})
e0 = ex.summary(a)["equity"]
print("before ex: last", last, "stop", st.stop_price, "equity", e0)
clock.advance(6); ex.tick()
q = feed.quote(sym); s = ex.summary(a)
print("after ex: last", q.last, "prev_close", q.prev_close, "stop status", st.status, "equity", s["equity"], "day_pnl", s["day_pnl"], "pending", s["pending_dividends"])

sec("9 option mark rounding across 2-for-1 split")
from app.options import chain
from app.options.symbology import build
ex, feed, clock = make_exchange(); deep(ex, feed, clock, "101.01")
a = ex.create_account("m", "margin", D("1000000")).id
exps = chain.expirations(date(2026,11,24)); 
osym = build("ABC", exps[3], "C", D("105"))
ex.submit(a, {"symbol": osym, "side": "buy", "qty": 50}); ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1001})
day = date(1970,1,1)+timedelta(days=int(clock.now()//86400)+1)
feed.schedule(split("ABC", day, 2, 1))
clock.set((int(clock.now()//86400)+1)*86400 - 1); ex.tick()
e0 = ex.summary(a)["equity"]; m0 = ex.mark(osym)
clock.advance(1); ex.tick()
s = ex.summary(a); print("equity before", e0, "after", s["equity"], "opt mark before", m0, "positions", {k:(str(v), str(ex.mark(k))) for k,v in ex.account(a).positions().items()})

sec("13 settings absurd values")
for v in ["1e-999999", "0.000000000000000000000000001", "50.0000000001", "-0", "1E1", " 5 ", "٥", "Infinity", True, [1], None, 1e400]:
    try: r = cfg.update(cfg.defaults(), {"margin_interest_rate": v}); print(repr(v), "->", r["margin_interest_rate"][:40])
    except Exception as e: print(repr(v), "rejected", type(e).__name__, str(e)[:60])

sec("11 interest: negative carry, display")
ex, feed, clock = make_exchange(); deep(ex, feed, clock, "100")
a = ex.create_account("m", "margin", D("100000")).id
ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1500})
clock.set(ts(2026,12,1,0)+5); ex.tick()
acc = ex.account(a); print("accrued after post", acc.life.accrued_interest, [ (t.memo, str(t.postings[0][1])) for t in acc.ledger.transactions if t.kind=="interest"], "summary accrued", ex.summary(a)["accrued_interest"])
