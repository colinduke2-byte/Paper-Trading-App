import sys; sys.path[:0]=["/home/user/wt/review-orders/backend","/home/user/wt/review-orders/backend/tests"]
from test_multileg import *
ex, feed, clock = make_exchange(); a = acct(ex, "cash", "20000")
os_ = ex.submit(a, {"symbol":"ABC","side":"buy","qty":1,"type":"limit","limit_price":"90","tif":"gtc",
                     "triggers":[{"symbol":"ABC","side":"buy","qty":10,"type":"market"}]})
child = os_[1]; ex.replace(a, child.id, {"qty": 100000})
move(ex, feed, clock, "ABC", "89.99"); move(ex, feed, clock, "ABC", "90.00"); move(ex, feed, clock, "ABC", "90.01")
s=ex.summary(a); print("cash account after triggered child: cash", s["cash"], held(ex,a), child.status, child.filled_qty)
ex, feed, clock = make_exchange(); a = acct(ex, "margin", "100000")
ex.submit(a, {"symbol":"ABC","side":"buy","qty":20})
A,B = ex.submit(a, {"oco":[{"symbol":"ABC","side":"buy","qty":10,"type":"limit","limit_price":"99","tif":"gtc"},
                           {"symbol":"ABC","side":"sell","qty":10,"type":"limit","limit_price":"101","tif":"gtc"}]})
clock.advance(1); feed.set_quote("ABC", D("98.99"), D("99.00"), D("99.00"), ask_size=D(4)); ex.tick()
print("A", A.status, A.filled_qty, "B qty", B.qty)
ex.replace(a, B.id, {"qty": 10})
move(ex, feed, clock, "ABC", "101.01"); 
print("A", A.status, A.filled_qty, "B", B.status, B.filled_qty, "group filled", A.filled_qty+B.filled_qty)
