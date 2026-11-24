import sys, json, tempfile, os, re, csv, io
sys.path.insert(0, "/home/user/wt/review-rc/backend"); sys.path.insert(0, "/home/user/wt/review-rc/backend/tests")
os.environ["PAPERDESK_ENV_FILE"] = "/nonexistent"
from decimal import Decimal as Dc
from datetime import timedelta
from fastapi.testclient import TestClient
from app.api.server import create_app
from app.core.clock import SimClock
from app.core import calendar as cal
from app.exchange import Exchange
from app.marketdata.synthetic import SyntheticFeed
from app.options import chain
from app.options.symbology import build

T0 = 1795500000.0 + 9*3600
clock = SimClock(T0); feed = SyntheticFeed(seed=7, now=T0)
ex = Exchange(feed, clock, data_dir=None)
acc = ex.create_account("Busy", "margin", Dc("100000")); a = acc.id
acc.settings["dividend_reinvestment"] = True
ex.watchlists = {"Main": ["MKTX"]}
def run(n=1, step=1):
    for _ in range(n):
        clock.advance(step); ex.tick()
run(3)
c = TestClient(create_app(ex, run_loop=False))
def post(body):
    r = c.post(f"/api/accounts/{a}/orders", json=body); 
    if r.status_code != 200: print("REJECT", body.get("symbol"), r.json())
    return r
post({"symbol": "MKTX", "side": "buy", "qty": 100}); post({"symbol": "NVLX", "side": "sell", "qty": 150})
post({"symbol": "ORBT", "side": "buy", "qty": "33.3333"}); post({"symbol": "RAIL", "side": "buy", "qty": 77})
post({"symbol": "PIPE", "side": "buy", "qty": 333}); post({"symbol": "BIOQ", "side": "sell", "qty": 400})
exp = chain.expirations(cal.ny_date(T0))[2]
spot = float(feed.quote("MKTX").last); k = round(spot/5)*5
post({"legs": [{"symbol": build("MKTX", exp, "C", Dc(k)), "side": "buy"}, {"symbol": build("MKTX", exp, "C", Dc(k+10)), "side": "sell"}], "qty": 3})
post({"legs": [{"symbol": build("MKTX", exp, "P", Dc(k-10)), "side": "sell"}, {"symbol": build("MKTX", exp, "P", Dc(k-20)), "side": "buy"},
               {"symbol": build("MKTX", exp, "C", Dc(k+15)), "side": "sell"}, {"symbol": build("MKTX", exp, "C", Dc(k+25)), "side": "buy"}], "qty": 2})
post({"symbol": build("TECX", exp, "P", Dc(round(float(feed.quote("TECX").last)/5)*5)), "side": "sell", "qty": 2})
post({"symbol": "TECX", "side": "buy", "qty": 10, "type": "limit", "limit_price": "100", "tif": "gtc"})
run(120); post({"symbol": "MKTX", "side": "sell", "qty": 40}); post({"symbol": "PIPE", "side": "sell", "qty": 100})
run(86400*3 // 600, 600)   # three days, 10-minute steps: dividends may land, interest accrues
run(5)
g = lambda p: c.get(p).json()
s = g(f"/api/accounts/{a}"); pos = g(f"/api/accounts/{a}/positions"); an = g(f"/api/accounts/{a}/analytics")
lst = [x for x in g("/api/accounts") if x["id"] == a][0]
perf = g(f"/api/accounts/{a}/performance")
d = cal.ny_date(ex.now()); st = g(f"/api/accounts/{a}/statements/{d.year:04d}-{d.month:02d}")
html = c.get(f"/api/accounts/{a}/statements/{d.year:04d}-{d.month:02d}/html").text
pcsv = list(csv.DictReader(io.StringIO(c.get(f"/api/accounts/{a}/export/positions").text)))
gains = g(f"/api/accounts/{a}/gains"); life = g(f"/api/accounts/{a}/lifecycle")
print("positions:", len(pos), "fills kinds:", sorted({f.kind for f in acc.fills}), "ledger kinds:", sorted({t.kind for t in acc.ledger.transactions}))
print("summary : equity", s["equity"], "cash", s["cash"], "unreal", s["unrealized_pnl"], "real", s["realized_pnl"], "fees", s["fees"], "long_mv", s["long_market_value"], "short_mv", s["short_market_value"], "pending_div", s["pending_dividends"], "day_pnl", s["day_pnl"], "total_pnl", s["total_pnl"])
print("list    : equity", lst["equity"])
sum_mv = sum(Dc(str(p["market_value"])) for p in pos); sum_un = sum(Dc(str(p["unrealized_pnl"])) for p in pos)
print("positions: sum mv", sum_mv, "(summary long+short", Dc(str(s["long_market_value"]))+Dc(str(s["short_market_value"])), ") sum unreal", sum_un, "sum day_pnl", sum(Dc(str(p["day_pnl"] or 0)) for p in pos))
print("cash+mv", Dc(str(s["cash"]))+sum_mv, "vs equity", s["equity"])
print("analytics summary:", an["summary"], "exposure totals", an["exposure"]["totals"], "trades overall", {k: an["trades"]["overall"][k] for k in ("trades","wins","losses","win_rate","total_gain","profit_factor")})
print("analytics returns: end_value", an["returns"]["end_value"], "twr", an["returns"]["time_weighted_return"], "maxdd", an["returns"]["max_drawdown"])
print("performance (old route): closed", perf["closed_trades"], "wins", perf["wins"], "win_rate", perf["win_rate"], "realized", perf["realized_pnl"], "total_return_pct", perf["total_return_pct"], "maxdd_pct", perf["max_drawdown_pct"], "pf", perf["profit_factor"])
print("statement: closing", st["closing"], "realized", st["realized_pnl"], "fees", st["fees"], "other_total", st["other_total"], "other_by_kind", st["other_by_kind"], "recon", st["reconciliation_difference"])
print("statement positions:", [(p["symbol"], p["qty"], p["market_value"]) for p in st["positions"]][:20])
print("real positions     :", [(p["symbol"], p["qty"], p["market_value"]) for p in pos][:20])
print("csv positions mv sum", sum(Dc(r["market_value"]) for r in pcsv), "csv header", list(pcsv[0].keys()))
print("gains totals", gains["totals"])
print("lifecycle", {k: life[k] for k in ("settled_cash","accrued_interest","accrued_borrow_fee","pending_dividends")})
print("html has source label?", [w for w in ("synthetic", "simulated", "model") if w in html.lower()])
open(os.path.join(os.path.dirname(__file__), "stmt.html"), "w").write(html)
print("float money in JSON summary sample:", repr(s["equity"]), type(s["equity"]))
