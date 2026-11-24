import sys; sys.path[:0]=["/home/user/wt/review-orders/backend","/home/user/wt/review-orders/backend/tests"]
from test_multileg import *
def setup():
    ex, feed, clock = make_exchange(ABC="100", XYZ="100"); a = acct(ex, "margin", "100000")
    ex.submit(a, {"legs": BULL_CALL, "qty": 10})
    s=ex.summary(a); n = int(s["buying_power"]/D("100.5"))
    ex.submit(a, {"symbol":"XYZ","side":"buy","qty":n,"type":"market"})
    move(ex, feed, clock, "XYZ", "70")
    s=ex.summary(a); print("equity", s["equity"], "maint", s["maintenance_requirement"], "init", s["initial_requirement"])
    return ex,feed,clock,a
ex,feed,clock,a=setup()
for name, req in (("buy back short C105 (pure risk reduction)", {"symbol":C105,"side":"buy","qty":10,"type":"market"}),
                  ("close whole spread as combo", {"legs":[leg(C100,"sell"),leg(C105,"buy")],"qty":10})):
    try: print(name, "->", ex.preview(a, req)["ok"] if "legs" not in req else ex.preview_multileg(a, req)["ok"])
    except Exception as e: print(name, "-> exception", e)
    try: print("   submit:", ex.submit(a, req)[0].status)
    except Exception as e: print("   submit REJECTED:", e)
    ex,feed,clock,a=setup()
