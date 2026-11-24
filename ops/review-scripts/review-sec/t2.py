import httpx, json, time, sys
B="http://127.0.0.1:18431"
def req(m, p, **k):
    t=time.time()
    try:
        with httpx.Client(base_url=B, timeout=120) as cc: r=cc.request(m,p,**k)
        print(m,p[:70], r.status_code, len(r.content), f"{time.time()-t:.2f}s", repr(r.text[:150])); return r
    except Exception as e: print(m,p,"EXC",repr(e)[:100])
a="A0005"
for p in ["", "/positions","/analytics","/performance","/gains","/statements","/statements/2026-11","/statements/2026-11/html","/export/ledger.csv","/export/fills.csv","/ledger","/lifecycle"]:
    req("GET", f"/api/accounts/{a}{p}")
# trail tiny
r=req("POST","/api/accounts/A0001/orders",json={"symbol":"MKTX","side":"sell","qty":"1","type":"trailing_stop","trail_amount":"1e-999999","tif":"gtc"})
r=req("POST","/api/accounts/A0001/orders",json={"symbol":"MKTX","side":"buy","qty":"1","type":"trailing_stop","trail_percent":"1e-999999","tif":"gtc"})
req("GET","/api/accounts/A0001/export/orders.csv")
req("GET","/api/accounts/A0001/orders")
# reset huge on a scratch account
r=req("POST","/api/accounts",json={"name":"scratch","starting_cash":1000}); sid=r.json()["id"]
req("POST",f"/api/accounts/{sid}/reset",json={"starting_cash":"1e999999"})
req("GET","/api/accounts")
req("GET",f"/api/accounts/{sid}/statements/2026-11/html")
req("GET",f"/api/accounts/{sid}/export/ledger.csv")
time.sleep(3)
