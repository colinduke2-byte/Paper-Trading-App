import sys; sys.path[:0]=["/tmp/claude-0/-home-user/bfc9a215-e653-5b8f-bc67-7e3e8af80e82/scratchpad/review-orders/be","/tmp/claude-0/-home-user/bfc9a215-e653-5b8f-bc67-7e3e8af80e82/scratchpad/review-orders/be/tests"]
from test_multileg import *
ex, feed, clock = make_exchange(); a = acct(ex)
L=[leg(C100,"buy"),leg(C105,"sell")]
try: ex.preview_multileg(a, {"legs":L,"type":"limit","limit_price":"1e30","price_effect":"debit"})
except Exception as e: print(type(e).__mro__, e)
os_=ex.submit(a, {"symbol":"ABC","side":"buy","qty":1,"triggers":[{"legs":L}]})
for o in os_: print(o.symbol,o.side,o.qty,o.type,o.role,o.legs,o.status)
# single order huge limit?
try: ex.preview(a, {"symbol":"ABC","side":"buy","qty":1,"type":"limit","limit_price":"1e30"})
except Exception as e: print("single:", type(e).__name__, e)
