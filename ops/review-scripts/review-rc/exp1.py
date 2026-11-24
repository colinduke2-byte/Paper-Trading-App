import sys, json, tempfile
sys.path.insert(0, "/home/user/wt/review-rc/backend"); sys.path.insert(0, "/home/user/wt/review-rc/backend/tests")
from datetime import date, timedelta, datetime, timezone
from decimal import Decimal as Dc
from app.core import calendar as cal
from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata.feed import SimFeed
from app.lifecycle.actions import dividend, split
from app.options import chain
from app.options.symbology import build
from app.analytics.statement import monthly_statement
from app.analytics.trades import gains_report
from helpers import T0, make_exchange, move

def stmt(ex, a):
    acc = ex.account(a)
    d = cal.ny_date(ex.now())
    marks = {s: ex.mark(s) for s in acc.positions()}
    return monthly_statement(acc, d.year, d.month, ex.now(), ex.summary(a)["equity"], marks)

print("=== 1a: 2:1 split, statement positions vs real positions")
ex, feed, clock = make_exchange(ABC="100.00")
a = ex.create_account("t", "margin", Dc("100000")).id
ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
today = ex.lifecycle.today()
feed.schedule(split("ABC", today + timedelta(days=1), 2, 1))
clock.advance(86400); ex.tick()
move(ex, feed, clock, "ABC", "50.00")
st = stmt(ex, a)
print("real positions:", ex.account(a).positions(), "| statement positions:", [(p["symbol"], p["qty"], p["market_value"]) for p in st["positions"]])
print("summary equity", ex.summary(a)["equity"], "recon diff", st["reconciliation_difference"])

print("=== 1b: 3:2 split with cash in lieu on 101 shares")
ex, feed, clock = make_exchange(ABC="100.00")
a = ex.create_account("t", "margin", Dc("100000")).id
ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 101})
feed.schedule(split("ABC", ex.lifecycle.today() + timedelta(days=1), 3, 2))
clock.advance(86400); ex.tick()
st = stmt(ex, a)
print("real positions:", ex.account(a).positions(), "| statement:", [(p["symbol"], p["qty"]) for p in st["positions"]])
print("other:", [(o["kind"], o["amount"]) for o in st["other"]], "realized", st["realized_pnl"], "recon diff", st["reconciliation_difference"])

print("=== 1c: reinvested dividend")
ex, feed, clock = make_exchange(ABC="100.00")
acc = ex.create_account("t", "margin", Dc("100000")); a = acc.id
acc.settings["dividend_reinvestment"] = True
ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 100})
t = ex.lifecycle.today()
feed.schedule(dividend("ABC", t + timedelta(days=1), t + timedelta(days=2), "1.00"))
clock.advance(86400); ex.tick(); clock.advance(86400); ex.tick()
st = stmt(ex, a)
print("positions", acc.positions(), "statement", [(p["symbol"], p["qty"]) for p in st["positions"]])
print("other:", [(o["kind"], o["amount"]) for o in st["other"]], "other_total", st["other_total"], "recon diff", st["reconciliation_difference"])
print("fills kinds", [f.kind for f in acc.fills])

print("=== 1d: cash account, long call closed at expiry for intrinsic")
ex, feed, clock = make_exchange(ABC="100.00")
acc = ex.create_account("t", "cash", Dc("2000")); a = acc.id
exp = chain.expirations(cal.ny_date(T0))[0]
sym = build("ABC", exp, "C", Dc("95"))
ex.submit(a, {"symbol": sym, "side": "buy", "qty": 1})
print("cash after buy", acc.cash)
from app.options.symbology import parse
clock.set(parse(sym).expiry_ts + 1); feed.set_quote("ABC", "109.99", "110.00", "110.00"); ex.tick()
d = cal.ny_date(ex.now())
st = monthly_statement(acc, d.year, d.month, ex.now(), ex.summary(a)["equity"], {})
print("fills", [(f.kind, f.side, f.qty, f.price) for f in acc.fills])
print("other:", [(o["kind"], o["amount"]) for o in st["other"]], "realized", st["realized_pnl"], "recon diff", st["reconciliation_difference"])
print("trades in stmt kinds", [t["kind"] for t in st["trades"]])
