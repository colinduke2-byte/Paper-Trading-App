import httpx, json, time, os, subprocess, sys, threading
B="http://127.0.0.1:18431"
c=httpx.Client(base_url=B, timeout=300)
pid=int(subprocess.check_output("pgrep -f '[p]ort 18431'", shell=True).split()[0])
def rss(): return int(open(f"/proc/{pid}/status").read().split("VmRSS:")[1].split()[0])//1024
def T(m,p,**k):
    t=time.time(); r=c.request(m,p,**k); print(f"{m} {p[:60]} -> {r.status_code} {len(r.content)}B {time.time()-t:.2f}s rss={rss()}MB {r.text[:90]!r}"); return r
print("rss start", rss())
# lru_cache memory: distinct 2MB 'symbols' in order preview
for i in range(40):
    r=c.post("/api/accounts/A0001/orders/preview", json={"symbol":("%04d"%i)+"A"*2_000_000,"side":"buy","qty":1})
print("after 40 x 2MB symbol previews:", r.status_code, len(r.content), "rss", rss())
time.sleep(3); print("3s later rss", rss())
# big body
T("POST","/api/accounts", content=b'{"name":"'+b"a"*60_000_000+b'","starting_cash":1}', headers={"content-type":"application/json"})
# decimal format bomb
r=T("POST","/api/accounts", json={"name":"bomb","starting_cash":"1e-60000000"}); bid=r.json()["id"]
T("GET",f"/api/accounts/{bid}/export/ledger.csv")
T("DELETE",f"/api/accounts/{bid}")
# status latency during the bomb export, in parallel
r=c.post("/api/accounts", json={"name":"bomb","starting_cash":"1e-150000000"}); bid=r.json()["id"]
def bg(): 
    try: T("GET",f"/api/accounts/{bid}/export/ledger.csv")
    except Exception as e: print("bg exc", repr(e)[:80])
th=threading.Thread(target=bg); th.start(); mx=0; peak=0
while th.is_alive():
    t=time.time()
    try: httpx.get(B+"/api/status", timeout=30)
    except Exception as e: print("status exc", repr(e)[:60])
    mx=max(mx,time.time()-t); peak=max(peak,rss()); time.sleep(0.05)
print(f"during 150M-digit export: worst /api/status latency {mx:.2f}s, peak rss {peak} MB")
T("DELETE",f"/api/accounts/{bid}")
time.sleep(2); print("rss end", rss())
