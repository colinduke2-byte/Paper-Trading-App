import sys; sys.path[:0]=["/home/user/wt/review-orders/backend","/home/user/wt/review-orders/backend/tests"]
from test_multileg import *
from app.risk.margin import _cover
pos = {opt("C",100,NEAR): D(-1), opt("C",110,FAR): D(-1), opt("C",90,FAR): D(1), opt("C",120,NEAR): D(1)}
marks = {"ABC": D(100)} | {s: D("5") for s in pos}
ev = margin.evaluate("cash", D(10**6), pos, marks)
print("cash eval:", ev.ok, ev.reason, ev.structural)
print(_cover(D(0), pos))
# valid pairing: Jan100 short with Jan120 long (loss 20 width), Mar110 short with Mar90 long (no loss)
print("margin req", margin.requirement(pos, marks))
