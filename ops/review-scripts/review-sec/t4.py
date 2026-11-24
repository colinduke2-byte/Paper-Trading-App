import httpx, json, time, os, subprocess, sys
B="http://127.0.0.1:18431"; D=sys.argv[1]
c=httpx.Client(base_url=B, timeout=300)
pid=int(subprocess.check_output("pgrep -f '[p]ort 18431'", shell=True).split()[0])
def rss(): return int(open(f"/proc/{pid}/status").read().split("VmRSS:")[1].split()[0])//1024
def st(): return os.path.getsize(D+"/state.json")
def lat():
    t=time.time(); c.get("/api/status"); return time.time()-t
for a in ("A0010","A0005"): print("del", a, c.delete(f"/api/accounts/{a}").status_code)
print("accounts", c.get("/api/accounts").status_code, "rss", rss(), "state", st())
# 1 journal growth
note="x"*4000
t=time.time(); N=1500
for i in range(N):
    r=c.post("/api/accounts/A0001/journal", json={"target_type":"order","target_id":"O00000001","note":note,"tags":["t"]})
    if r.status_code!=200: print(r.status_code, r.text[:200]); break
el=time.time()-t; time.sleep(2.5)
print(f"journal: {N} posts in {el:.1f}s -> state.json {st()/1e6:.1f} MB, rss {rss()} MB, status latency {lat()*1000:.0f} ms")
t=time.time(); r=c.get("/api/accounts/A0001/journal"); print("journal list", len(r.content), f"{time.time()-t:.2f}s")
# 2 accounts
t=time.time(); N=1500
for i in range(N):
    r=c.post("/api/accounts", json={"name":"n"*40,"starting_cash":1})
el=time.time()-t; time.sleep(2.5)
t=time.time(); r=c.get("/api/accounts"); ta=time.time()-t
print(f"accounts: {N} created in {el:.1f}s -> state {st()/1e6:.1f} MB, rss {rss()}, GET /api/accounts {len(r.content)/1e6:.1f} MB in {ta:.2f}s")
# 3 alerts
t=time.time(); N=5000
for i in range(N):
    r=c.post("/api/alerts", json={"symbol":"MKTX","op":"above","price":"99999"})
el=time.time()-t; time.sleep(2.5)
print(f"alerts: {N} in {el:.1f}s -> state {st()/1e6:.1f} MB, rss {rss()}, GET alerts {len(c.get('/api/alerts').content)/1e6:.1f} MB")
# watchlists
for i in range(2000): c.put(f"/api/watchlists/w{i}", json=["MKTX"]*100)
time.sleep(2.5); print("watchlists 2000 -> state", st()/1e6, "MB")
