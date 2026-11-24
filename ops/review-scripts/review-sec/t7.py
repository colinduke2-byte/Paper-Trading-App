import httpx, json, time, os, sys, threading, gzip
B="http://127.0.0.1:18431"; D=sys.argv[1]
c=httpx.Client(base_url=B, timeout=300)
def T(m,p,**k):
    t=time.time()
    try: r=c.request(m,p,**k); print(f"{m} {p[:70]} -> {r.status_code} {len(r.content)}B {time.time()-t:.2f}s {r.text[:160]!r}"); return r
    except Exception as e: print(m,p,"EXC",repr(e)[:100])
# concurrency: 6 threads submit/cancel market orders for 8 s
r=c.post("/api/accounts", json={"name":"race","starting_cash":"100000000"}); aid=r.json()["id"]
stop=time.time()+8; codes={}
def w():
    cc=httpx.Client(base_url=B, timeout=60)
    while time.time()<stop:
        r=cc.post(f"/api/accounts/{aid}/orders", json={"symbol":"MKTX","side":"buy","qty":"1","type":"market"})
        codes[r.status_code]=codes.get(r.status_code,0)+1
        r=cc.post(f"/api/accounts/{aid}/orders", json={"symbol":"MKTX","side":"sell","qty":"1","type":"limit","limit_price":"9999","tif":"gtc"})
        codes[r.status_code]=codes.get(r.status_code,0)+1
        cc.delete(f"/api/accounts/{aid}/orders")
ths=[threading.Thread(target=w) for _ in range(6)]; [t.start() for t in ths]; [t.join() for t in ths]
print("race codes", codes)
a=c.get(f"/api/accounts/{aid}").json(); pos=c.get(f"/api/accounts/{aid}/positions").json(); fills=c.get(f"/api/accounts/{aid}/fills?limit=5000").json()
print("position qty", [p["qty"] for p in pos], "fills", len(fills), "buy fills", sum(1 for f in fills if f["side"]=="buy"))
T("GET", f"/api/accounts/{aid}/analytics"); T("GET", f"/api/accounts/{aid}/statements/2026-11/html"); T("GET", f"/api/accounts/{aid}/gains")
c.delete(f"/api/accounts/{aid}")
# option chain
ex=c.get("/api/options/expirations/MKTX").json(); T("GET", f"/api/options/chain/MKTX?expiry={ex[-1]['date']}")
T("GET","/api/bars/MKTX?tf=1m&limit=5000"); T("GET","/api/screener"); T("GET","/api/movers")
# replay
ss=T("GET","/api/replay/sessions").json(); sid=ss[0]["id"]; print([ (s["id"],s["size_bytes"]) for s in ss])
for bad in ["../../etc/passwd","..","/etc/passwd","a/b","sample\x00","C:\\x", sid+"/", ""]:
    T("POST","/api/replay/start", json={"session":bad})
T("POST","/api/replay/start", json={"session":sid,"starting_cash":"1e-999999"})
T("POST","/api/replay/exit")
T("POST","/api/replay/start", json={"session":sid,"speed":60})
st=c.get("/api/replay/status").json(); print("span", st["end"]-st["start"])
def lat():
    mx=0
    while not done[0]:
        t=time.time(); httpx.get(B+"/api/status", timeout=120); mx=max(mx,time.time()-t); time.sleep(0.1)
    print(f"worst /api/status latency during seek: {mx:.2f}s")
done=[False]; th=threading.Thread(target=lat); th.start()
T("POST","/api/replay/seek", json={"offset":1e12}); T("POST","/api/replay/seek", json={"offset":"nan"}); T("POST","/api/replay/seek", json={"time":[1]})
T("POST","/api/replay/seek", json={"offset":st["end"]-st["start"]})
done[0]=True; th.join()
T("POST","/api/replay/step", json={"seconds":1e99}); T("POST","/api/replay/speed", json={"speed":"inf"})
T("POST","/api/replay/exit")
# crafted session files
def wr(name, lines, raw=None):
    with gzip.open(os.path.join(D,"sessions",name+".pdsession.gz"),"wb") as f: f.write(raw if raw is not None else ("\n".join(json.dumps(l) if not isinstance(l,str) else l for l in lines)+"\n").encode())
H={"format":"paperdesk-session","version":1,"date":"2026-01-05","start":1767623400,"symbols":[{"symbol":"ZZZ","name":"<img src=x onerror=alert(1)>","sector":"S"}],"initial":{"ZZZ":{"bid":"10","ask":"10.1","last":"10","prev_close":"10"}}}
wr("bad-symbols", [dict(H, symbols=[1,2])])
T("GET","/api/replay/sessions")
os.remove(os.path.join(D,"sessions","bad-symbols.pdsession.gz"))
wr("bad-nan", [H, ["Q",1767623401,"ZZZ","NaN","NaN","1","1"], ["T",1767623402,"ZZZ","1e999999","1"], {"end":{"events":2,"end":1767623500}}])
wr("bomb", None, raw=b'{"format":"paperdesk-session","version":1,"pad":"'+b"a"*300_000_000+b'"}\n')
print("bomb file size", os.path.getsize(os.path.join(D,"sessions","bomb.pdsession.gz")))
T("GET","/api/replay/sessions")
os.remove(os.path.join(D,"sessions","bomb.pdsession.gz"))
T("POST","/api/replay/start", json={"session":"bad-nan","play":True}); time.sleep(3)
T("GET","/api/quotes?symbols=ZZZ"); T("GET","/api/symbols"); T("GET","/api/accounts")
T("POST","/api/replay/exit"); T("GET","/api/accounts/A0001")
os.remove(os.path.join(D,"sessions","bad-nan.pdsession.gz"))
