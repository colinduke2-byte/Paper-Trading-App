import sys; sys.path[:0]=["/tmp/claude-0/-home-user/bfc9a215-e653-5b8f-bc67-7e3e8af80e82/scratchpad/review-orders/be","/tmp/claude-0/-home-user/bfc9a215-e653-5b8f-bc67-7e3e8af80e82/scratchpad/review-orders/be/tests"]
from test_multileg import *
from app.risk.margin import _cover
pos = {opt("C",100,NEAR): D(-1), opt("C",110,FAR): D(-1), opt("C",90,FAR): D(1), opt("C",120,NEAR): D(1)}
marks = {"ABC": D(100)} | {s: D("5") for s in pos}
ev = margin.evaluate("cash", D(10**6), pos, marks)
print("cash eval:", ev.ok, ev.reason, ev.structural)
print(_cover(D(0), pos))
# valid pairing: Jan100 short with Jan120 long (loss 20 width), Mar110 short with Mar90 long (no loss)
print("margin req", margin.requirement(pos, marks))
