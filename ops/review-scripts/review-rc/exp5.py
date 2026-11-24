import sys, tempfile
sys.path.insert(0, "/home/user/wt/review-rc/backend"); sys.path.insert(0, "/home/user/wt/review-rc/backend/tests")
from decimal import Decimal as Dc
from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata.feed import SimFeed
from app.analytics.common import equity_points
from app.analytics.returns import report
from helpers import T0

print("=== A: live-like feed restart: equity sample before the first quotes")
tmp = tempfile.mkdtemp(); clock = SimClock(T0); feed = SimFeed(); ex = Exchange(feed, clock, data_dir=tmp)
feed.set_quote("ABC", "99.99", "100.00", "100.00"); ex.tick()
acc = ex.create_account("t", "margin", Dc("100000")); a = acc.id
ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 900})
clock.advance(61); feed.set_quote("ABC", "99.99", "100.00", "100.00"); ex.tick(); ex.save(force=True)
clock2 = SimClock(T0 + 3600); feed2 = SimFeed({"ABC": {"symbol": "ABC", "name": "ABC", "sector": "", "vol": 0.3}})
ex2 = Exchange(feed2, clock2, data_dir=tmp); ex2.load(); ex2.tick()          # first tick, provider has not answered yet
clock2.advance(2); feed2.set_quote("ABC", "99.99", "100.00", "100.00"); ex2.tick()
acc2 = ex2.account(a); s = ex2.summary(a)
print("equity_history:", acc2.equity_history, "| live equity", s["equity"], "day_pnl", s["day_pnl"])
r = report(equity_points(acc2, ex2.now(), s["equity"]))
print("Reports max drawdown:", r["max_drawdown"], "twr", r["time_weighted_return"])

print("=== B: margin call opened, then the live exchange is not ticked for 49h (replay / app closed), default 48h grace")
clock = SimClock(T0); feed = SimFeed(); ex = Exchange(feed, clock)
feed.set_quote("ABC", "99.99", "100.00", "100.00"); ex.tick()
acc = ex.create_account("t", "margin", Dc("100000")); a = acc.id
ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1900})
clock.advance(5); feed.set_quote("ABC", "59.99", "60.00", "60.00"); ex.tick()
print("call since", acc.life.margin_call_since is not None, "positions", acc.positions())
clock.advance(49 * 3600); feed.set_quote("ABC", "59.99", "60.00", "60.00"); ex.tick()   # first tick after returning
print("after return: positions", acc.positions(), "log:", [m[:60] for _, k, m in acc.life.log][-2:])
