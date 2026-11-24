import sys, json, os, tempfile; sys.path[:0]=["/home/user/wt/review-orders/backend","/home/user/wt/review-orders/backend/tests"]
from test_multileg import *
# A. replace a held triggered child: no risk check
ex, feed, clock = make_exchange(); a = acct(ex, "cash", "20000")
os_ = ex.submit(a, {"symbol":"ABC","side":"buy","qty":1,"type":"limit","limit_price":"90","tif":"gtc",
                     "triggers":[{"symbol":"ABC","side":"buy","qty":10,"type":"market"}]})
child = os_[1]; print("child", child.status, child.role, child.qty)
ex.replace(a, child.id, {"qty": 100000}); print("child qty after replace", child.qty)
move(ex, feed, clock, "ABC", "89.99")
s=ex.summary(a); print("after trigger: cash", s["cash"], held(ex,a))
# B. OCO overfill via replace
ex, feed, clock = make_exchange(); a = acct(ex, "margin", "100000")
os_ = ex.submit(a, {"oco":[{"symbol":"ABC","side":"buy","qty":10,"type":"limit","limit_price":"99","tif":"gtc"},
                           {"symbol":"ABC","side":"buy","qty":10,"type":"limit","limit_price":"98","tif":"gtc"}]})
A,B = os_
move(ex, feed, clock, "ABC", "99", ask_size=4) if False else None
# partial-fill A with 4 shares
clock.advance(1); feed.set_quote("ABC", D("98.99"), D("99.00"), D("99.00"), ask_size=D(4)); ex.tick()
print("A", A.status, A.filled_qty, "B qty", B.qty)
ex.replace(a, B.id, {"qty": 10})
clock.advance(1); feed.set_quote("ABC", D("97.99"), D("98.00"), D("98.00")); ex.tick()
print("A", A.status, A.filled_qty, "B", B.status, B.filled_qty, "total position", held(ex,a))
