import sys, random; sys.path[:0]=["/home/user/wt/review-orders/backend","/home/user/wt/review-orders/backend/tests"]
from test_multileg import *
from app.risk.margin import _cover, requirement
random.seed(1)
S=D(100)
U=[opt(k,s,e) for k in "CP" for s in (90,95,100,105,110) for e in (NEAR,FAR)]
def mark(sym):
    c=parse(sym); intr=max(S-c.strike,0) if c.kind=="C" else max(c.strike-S,0)
    return D(intr)+(D("0.5") if c.expiry==NEAR else D("1.5"))
grid=[D(p) for p in range(0,301)]
def val(pos,p):
    t=D(0)
    for s,q in pos.items():
        c=parse(s)
        if c is None: t+=q*p
        else: t+=q*100*(max(p-c.strike,0) if c.kind=="C" else max(c.strike-p,0))
    return t
bad=design=0; examples=[]
for it in range(40000):
    syms=random.sample(U, random.randint(1,4))
    pos={s:D(random.randint(-3,3)) for s in syms}
    st=random.choice([0,0,0,random.randint(-3,3)*100])
    if st: pos["ABC"]=D(st)
    pos={s:q for s,q in pos.items() if q}
    if not pos: continue
    marks={"ABC":S}|{s:mark(s) for s in pos if parse(s)}
    mv=sum((q*(marks[s] if not parse(s) else marks[s]*100) for s,q in pos.items()),D(0))
    vals=[val(pos,p) for p in grid]
    # unbounded? check slope at high end
    if vals[-1]<vals[-2]: continue
    loss=mv-min(vals)
    if loss<=0: continue
    req=requirement(pos,marks)
    shares=pos.get("ABC",D(0))
    opts={s:q for s,q in pos.items() if parse(s)}
    _,groups,naked,_=_cover(shares,opts)
    if st<0: continue
    if req < loss - D("0.01"):
        if naked or st: design+=1
        else:
            bad+=1
            if len(examples)<5: examples.append((pos,req,loss))
print("under-stated defined-risk (no naked, no stock):",bad," with naked/stock (Reg T by design):",design)
for e in examples: print(e)
# cash account: obligation check -- cash = secured exactly; min cash at expiry (physical settlement) 
neg=0; ex2=[]
for it in range(20000):
    syms=[s for s in random.sample(U, random.randint(1,4)) if parse(s).expiry==NEAR]
    pos={s:D(random.randint(-3,3)) for s in syms}; pos={s:q for s,q in pos.items() if q}
    if not pos: continue
    marks={"ABC":S}|{s:mark(s) for s in pos}
    ev=margin.evaluate("cash",D(10**7),pos,marks)
    if not ev.ok or not any(q<0 for q in pos.values()) or not any(q>0 for q in pos.values()): continue
    cash=ev.requirement
    # physical settlement at each price: calls ITM -> buy/sell at strike
    for p in grid:
        c2=cash; stock=D(0)
        for s,q in pos.items():
            c=parse(s); itm=(p-c.strike) if c.kind=="C" else (c.strike-p)
            if itm>=D("0.01") and q<0:
                buys=(c.kind=="C")==(q>0); n=abs(q)*100
                stock+= n if buys else -n; c2+= -n*c.strike if buys else n*c.strike
        if c2<0 or stock<0:
            neg+=1
            if len(ex2)<3: ex2.append((pos,cash,p,c2,stock))
            break
print("cash books (secured exactly) that end with negative cash or short stock after expiry:",neg)
for e in ex2: print(e)
