import sys; sys.path[:0]=["/home/user/wt/review-orders/backend","/home/user/wt/review-orders/backend/tests"]
from test_multileg import *
# 1. cash account put credit spread pinned between strikes
ex, feed, clock, a, cash = open_spread([leg(P100, "sell"), leg(P95, "buy")], qty=1, type="cash", cash="600")
print("cash after open", cash, held(ex,a))
at_expiry(ex, feed, clock, "97")
print("after pin expiry: cash", ex.summary(a)["cash"], held(ex,a), ex.summary(a)["equity"])
# 1b. call credit spread pinned in cash account -> short stock
ex, feed, clock, a, cash = open_spread([leg(C100, "sell"), leg(C105, "buy")], qty=1, type="cash", cash="600")
at_expiry(ex, feed, clock, "103")
print("call credit pinned: cash", ex.summary(a)["cash"], held(ex,a))
# 2. put calendar in cash, secured 0
far = opt("P", 100, FAR)
ex, feed, clock = make_exchange(); a = acct(ex, "cash", "2000")
try:
    [o] = ex.submit(a, {"legs":[leg(P100,"sell"), leg(far,"buy")], "qty":1}); print("calendar", o.status, ex.summary(a)["cash"])
    at_expiry(ex, feed, clock, "80")
    print("calendar after near expiry: cash", ex.summary(a)["cash"], held(ex,a))
except Exception as e: print("rej", e)
