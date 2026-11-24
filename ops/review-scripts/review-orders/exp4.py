import sys; sys.path[:0]=["/home/user/wt/review-orders/backend","/home/user/wt/review-orders/backend/tests"]
from test_multileg import *
ex, feed, clock = make_exchange(ABC="100", XYZ="100"); a = acct(ex, "margin", "100000")
ex.submit(a, {"symbol":"ABC","side":"buy","qty":100})
ex.submit(a, {"legs": [leg(C100,"buy"), leg(C105,"sell")], "qty": 1})
s=ex.summary(a); n = int(s["buying_power"]/D("100.5"))
ex.submit(a, {"symbol":"XYZ","side":"buy","qty":n})
move(ex, feed, clock, "XYZ", "70")
s=ex.summary(a); print("equity", s["equity"], "init", s["initial_requirement"], "positions", held(ex,a))
# nonmarketable-looking but working orders: use limit sells far above so they rest, then fill both
o1 = ex.submit(a, {"symbol":"ABC","side":"sell","qty":100,"type":"limit","limit_price":"101","tif":"gtc"})[0]
o2 = ex.submit(a, {"symbol":C100,"side":"sell","qty":1,"type":"limit","limit_price":"50","tif":"gtc"})[0]
print("accepted:", o1.status, o2.status)
# Does an opening naked call get rejected here? (control)
try: ex.submit(a, {"symbol":C105,"side":"sell","qty":1,"type":"limit","limit_price":"50","tif":"gtc"}); print("naked call accepted")
except Exception as e: print("control naked call rejected:", e)
