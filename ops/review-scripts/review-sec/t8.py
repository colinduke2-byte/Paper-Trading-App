import httpx, json, time
B="http://127.0.0.1:18431"; c=httpx.Client(base_url=B, timeout=60)
def T(m,p,**k):
    try: r=c.request(m,p,**k); print(f"{m} {p[:60]} -> {r.status_code} {len(r.content)}B {r.text[:130]!r}"); return r
    except Exception as e: print(m,p,"EXC",repr(e)[:100])
T("GET","/api/accounts/A0001", headers={"Origin":"http://evil.example","Sec-Fetch-Site":"cross-site","Sec-Fetch-Mode":"no-cors"})
r=T("GET","/api/accounts/A0001/export/ledger.csv", headers={"Sec-Fetch-Site":"cross-site"}); print({k:v for k,v in r.headers.items() if "cross-origin" in k})
r=c.post("/api/accounts/A0001/orders", json={"symbol":"MKTX","side":"buy","qty":"1","type":"limit","limit_price":"400","tif":"gtc"}); oid=r.json()[0]["id"]; print(oid)
T("PATCH",f"/api/accounts/A0001/orders/{oid}", json={"limit_price":"1e-999999"})
T("PATCH",f"/api/accounts/A0001/orders/{oid}", json={"qty":"1e-999999"})
T("PATCH",f"/api/accounts/A0001/orders/{oid}", json={"limit_price":"abc"})
T("PATCH",f"/api/accounts/A0001/orders/{oid}", json=[1])
T("POST","/api/accounts/A0001/orders", json=[1])
T("POST","/api/accounts/A0001/orders", json={"symbol":"MKTX","side":"buy","qty":"1","client_id":{"a":1}})
T("POST","/api/accounts/A0001/orders", json={"symbol":"MKTX","side":"buy","qty":"1","type":["x"]})
T("POST","/api/accounts/A0001/orders", json={"symbol":"MKTX","side":"buy","qty":"1","tif":{"x":1}})
T("POST","/api/accounts/A0001/orders", json={"symbol":"MKTX","side":"buy","qty":"1","tif":"gtd","good_till_date":{"x":1}})
T("POST","/api/accounts/A0001/orders", json={"symbol":"MKTX","side":"buy","qty":"1","oco":"x"})
T("POST","/api/accounts/A0001/orders", json={"symbol":"MKTX","side":"buy","qty":"1","triggers":[{"symbol":"MKTX","side":"sell","qty":"1","type":"limit","limit_price":"1e-999999"}]})
T("POST","/api/accounts/A0001/multileg/preview", json={"legs":"x"})
T("POST","/api/accounts/A0001/multileg/preview", json={"legs":[{"symbol":"MKTX","side":"buy","ratio":"1e999999"},{"symbol":"MKTX","side":"sell","ratio":{"a":1}}],"qty":1})
ex=c.get("/api/options/expirations/MKTX").json()[2]["date"]; ch=c.get(f"/api/options/chain/MKTX?expiry={ex}").json()["rows"]; 
row=ch[len(ch)//2]; print({k:(v if not isinstance(v,dict) else list(v)[:4]) for k,v in row.items()})
