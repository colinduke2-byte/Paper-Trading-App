import sys; sys.path[:0]=["/home/user/wt/review-orders/backend","/home/user/wt/review-orders/backend/tests"]
from test_multileg import *
# 3. debit spread in margin account under requirement: buy back the short leg
ex, feed, clock = make_exchange(); a = acct(ex, "margin", "100000")
ex.submit(a, {"legs": BULL_CALL, "qty": 10})
# Load up on stock to use all buying power, then drop the price to create a margin call
s=ex.summary(a); print("bp", s["buying_power"])
n = int(s["buying_power"]/D("100.5"))
ex.submit(a, {"symbol":"ABC","side":"buy","qty":n,"type":"market"})
move(ex, feed, clock, "ABC", "70")
s=ex.summary(a); print("equity", s["equity"], "maint", s.get("maintenance_requirement"), "init", s["initial_requirement"])
for req in ({"symbol":C105,"side":"buy","qty":10,"type":"market"},):
    try: print("buy back short leg:", ex.submit(a, req)[0].status)
    except Exception as e: print("buy back short leg REJECTED:", e)
try: print("sell stock:", ex.submit(a, {"symbol":"ABC","side":"sell","qty":10,"type":"market"})[0].status)
except Exception as e: print("sell stock rejected:", e)
# closing whole spread as a multileg
try: print("close spread combo:", ex.submit(a, {"legs":[leg(C100,"sell"),leg(C105,"buy")],"qty":10})[0].status)
except Exception as e: print("close combo rejected:", e)
