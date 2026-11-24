import sys, random
sys.path.insert(0, "/home/user/wt/review-lifecycle/backend/tests"); sys.path.insert(0, "/home/user/wt/review-lifecycle/backend")
from datetime import date
from decimal import Decimal as D
from app.ledger.account import Account
from app.core.models import Fill
from app.lifecycle.actions import split
from app.lifecycle import splits
from app.core.money import cents
rng = random.Random(1)
bad = 0; worst = D(0)
for it in range(40000):
    a = Account("A1", "t", "margin", D("1000000"), 0.0)
    short = rng.random() < 0.4
    frac = rng.random() < 0.4
    n = rng.randint(1, 5)
    for i in range(n):
        q = D(rng.randint(1, 3000)) / (D(10000) if frac and rng.random()<.5 else (D(100) if frac else D(1)))
        q = q.quantize(D("0.0001"))
        if q == 0: continue
        a.apply_fill(Fill(f"F{i}", None, "A1", "ABC", "sell" if short else "buy", q, D(rng.randint(1, 50000))/100, float(i)), 1)
    if rng.random() < .5 and a.position("ABC") != 0:
        q = (abs(a.position("ABC")) * D(rng.randint(1, 99)) / 100).quantize(D("0.0001"))
        if q > 0:
            a.apply_fill(Fill("FX", None, "A1", "ABC", "buy" if short else "sell", q, D(rng.randint(1, 50000))/100, 9.0), 1)
    pos0 = a.position("ABC")
    if pos0 == 0: continue
    new, old = rng.choice([(2,1),(3,1),(4,1),(3,2),(1,2),(1,10),(1,50),(5,4),(7,3),(2,3),(10,1),(1,1000)])
    price = D(rng.randint(1, 100000))/100
    act = split("ABC", date(2026,1,2), new, old)
    cash0, basis0, real0 = a.cash, a.cost_basis("ABC"), a.realized
    v0 = cash0 + pos0*price
    res = splits.split_stock(a, act, price, 10.0)
    pos1 = a.position("ABC"); adj = price*old/new
    v1 = a.cash + pos1*adj
    errs = []
    if sum(a.ledger.balances.values()) != 0: errs.append("unbalanced")
    if a.ledger.balance("pos:ABC") != a.cost_basis("ABC"): errs.append(f"pos acct {a.ledger.balance('pos:ABC')} != basis {a.cost_basis('ABC')}")
    if abs(v1 - v0) > D("0.011"): errs.append(f"value {v0}->{v1}")
    for l in a.lots.get("ABC", []):
        if l.cost < 0 or (l.qty > 0) != (pos0 > 0) or l.qty != l.qty.quantize(D("0.0001")): errs.append(f"lot {l}")
    exact = abs(pos0)*new/old
    if abs(pos1) > exact or exact - abs(pos1) >= 1: errs.append(f"qty {pos0}->{pos1} exact {exact}")
    # realized consistency: cash change + basis change == realized change
    if (a.cash - cash0) + (a.cost_basis("ABC") - basis0) != (a.realized - real0): errs.append("realized mismatch")
    # sign check: long gets cash >= 0, short pays
    dc = a.cash - cash0
    if (pos0 > 0 and dc < 0) or (pos0 < 0 and dc > 0): errs.append(f"cash sign {dc}")
    worst = max(worst, abs(v1-v0))
    if errs:
        bad += 1
        if bad <= 8: print(it, pos0, f"{new}-for-{old}", price, errs, res and res["cash_in_lieu"])
print("bad", bad, "worst value drift", worst)
