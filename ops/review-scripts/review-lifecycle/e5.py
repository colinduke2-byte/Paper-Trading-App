import sys, random
sys.path.insert(0, "/home/user/wt/review-lifecycle/backend/tests"); sys.path.insert(0, "/home/user/wt/review-lifecycle/backend")
from datetime import date
from decimal import Decimal
from helpers import D, make_exchange, move
from app.core.models import Fill
from app.options import chain
from app.options.symbology import build, parse
from app.risk import margin
rng = random.Random(3)
exps = chain.expirations(date(2026,11,24))
stats = {"inc":0, "stuck":0, "naked_created":0, "runs":0, "over":0}
shown = 0
for it in range(1500):
    ex, feed, clock = make_exchange(ABC="100", XYZ="50")
    for s, p in (("ABC","100"),("XYZ","50")):
        move(ex, feed, clock, s, p, bid_size=1000000, ask_size=1000000)
    a = ex.create_account("m", "margin", D("50000")); a.settings["margin_call_grace_hours"]="0"
    k = 0
    def put(sym, qty):
        global k; k += 1
        if qty == 0: return
        px = ex.mark(sym)
        if px <= 0: return
        a.apply_fill(Fill(f"Z{k}", None, a.id, sym, "buy" if qty>0 else "sell", D(abs(qty)), px, clock.now()), 100 if parse(sym) else 1)
    for s in ("ABC","XYZ"):
        r = rng.random()
        if r < .6: put(s, rng.randint(1, 1500))
        elif r < .8: put(s, -rng.randint(1, 800))
        for _ in range(rng.randint(0,2)):
            px = D(100 if s=="ABC" else 50)
            strike = px + D(rng.choice([-10,-5,0,5,10]))
            osym = build(s, exps[rng.randint(1,4)], rng.choice("CP"), strike)
            put(osym, rng.choice([-1,1])*rng.randint(1,12))
    # shock
    move(ex, feed, clock, "ABC", str(rng.choice([55,70,85,100,120,150])), bid_size=1000000, ask_size=1000000)
    move(ex, feed, clock, "XYZ", str(rng.choice([25,35,50,65,80])), bid_size=1000000, ask_size=1000000)
    pos, marks, eq, maint = ex.lifecycle._state(a)
    if not pos or eq >= maint or a.life.margin_call_since is None and False: continue
    stats["runs"] += 1
    before = dict(pos)
    def naked(pos):
        out = 0
        for s,q in pos.items():
            c = parse(s)
            if c and q<0 and c.kind=="C":
                out += max(0, -q*100 - max(pos.get(c.underlying, 0), 0))
        return out
    nk0 = naked(pos)
    steps = ex.lifecycle.liquidate(a, clock.now())
    pos2, marks2, eq2, maint2 = ex.lifecycle._state(a)
    msg = []
    for st in steps:
        if st["requirement_after"] > st["requirement_before"]: stats["inc"] += 1; msg.append(("REQ UP", st["symbol"], st["qty"], st["requirement_before"], st["requirement_after"]))
    if pos2 and eq2 < maint2: stats["stuck"] += 1; msg.append(("stuck", eq2, maint2, {k:str(v) for k,v in pos2.items()}))
    if naked(pos2) > nk0: stats["naked_created"] += 1; msg.append(("naked calls up", nk0, naked(pos2)))
    if pos2 and eq2 - maint2 > (maint - eq) and eq2 - maint2 > 2000: stats["over"] += 1; msg.append(("overshoot", "deficit", maint-eq, "excess after", eq2-maint2))
    if msg and any(m_[0]=="overshoot" for m_ in msg):
        shown += 1
        print(it, {k:str(v) for k,v in before.items()}, "eq", eq, "maint", maint); 
        for m_ in msg: print("    ", m_)
        print("     steps", [(s["symbol"], s["side"], str(s["qty"])) for s in steps])
print(stats)
