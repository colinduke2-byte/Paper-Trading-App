import httpx, json, time, sys
B="http://127.0.0.1:18431"
class C:
    def __getattr__(self, m):
        def f(*a, **k):
            try:
                with httpx.Client(base_url=B, timeout=60) as cc: return getattr(cc,m)(*a,**k)
            except Exception as e:
                class R: status_code=-1; headers={"content-security-policy":"x"*50}; text=repr(e)
                return R()
        return f
c=C()
def show(tag, r, n=200):
    print(tag, r.status_code, r.headers.get("content-type"), repr(r.text[:n]))
accts=c.get("/api/accounts").json(); aid=accts[0]["id"]; print(aid)
# CSRF style: content types
for ct in ["text/plain","application/x-www-form-urlencoded","application/json"]:
    r=c.post("/api/accounts", content=b'{"name":"csrf","starting_cash":5}', headers={"content-type":ct}); show("ct "+ct, r)
# with cross Origin
r=c.post(f"/api/accounts/{aid}/reset", headers={"Origin":"http://evil.example"}); show("xorigin reset", r)
r=c.post(f"/api/accounts/{aid}/settings/reset", headers={"Origin":"null"}); show("null origin", r)
r=c.post(f"/api/accounts/{aid}/settings/reset", headers={"Sec-Fetch-Site":"cross-site"}); show("sfs", r)
r=c.post(f"/api/accounts/{aid}/settings/reset"); show("no origin", r, 60)
r=c.get("/api/status", headers={"Origin":"http://evil.example"}); print("cors hdrs", {k:v for k,v in r.headers.items() if 'access-control' in k})
r=c.options("/api/accounts", headers={"Origin":"http://evil.example","Access-Control-Request-Method":"POST"}); show("preflight", r); print({k:v for k,v in r.headers.items() if 'access-control' in k})
r=c.get("/api/status", headers={"Host":"evil.example"}); show("host", r)
# bad numbers
for body in [{"name":"x","starting_cash":"abc"},{"name":"x","starting_cash":{"a":1}},{"name":"x","starting_cash":"NaN"},{"name":"x","starting_cash":"1e-999999"},{"name":{"a":1},"type":{"x":[1]},"starting_cash":1},{"name":"x","type":"bogus"},{"name":"<script>alert(1)</script>","type":"margin"}]:
    r=c.post("/api/accounts", json=body); show("acct "+json.dumps(body)[:50], r, 300)
r=c.post("/api/alerts", json={"symbol":"MKTX","op":"above","price":"abc"}); show("alert abc", r)
r=c.post("/api/alerts", json={"symbol":"MKTX","op":"above","price":"1e-999999"}); show("alert tiny", r)
r=c.post("/api/alerts", json={"symbol":"MKTX","op":["x"],"price":"1"}); show("alert op list", r)
r=c.post("/api/alerts", json={"symbol":"MKTX","op":{"x":1},"price":"1"}); show("alert op dict", r)
r=c.get("/api/alerts"); show("alerts", r, 400)
# nesting
deep="["*100000+"]"*100000
r=c.post("/api/accounts", content=deep.encode(), headers={"content-type":"application/json"}); show("deep", r)
r=c.post("/api/accounts", content=b'{"name":"n","starting_cash":'+b"9"*5000+b'}', headers={"content-type":"application/json"}); show("bigint", r)
r=c.post("/api/accounts", content=b'{"name":"n","starting_cash":NaN}', headers={"content-type":"application/json"}); show("NaN", r)
r=c.post("/api/accounts", content=b'{"name":"n","starting_cash":1e999}', headers={"content-type":"application/json"}); show("inf", r)
r=c.post("/api/accounts", content=b'{"name":"a","name":"b","starting_cash":5,"starting_cash":7}', headers={"content-type":"application/json"}); show("dup", r)
r=c.get("/api/screener?min_price=nan&max_pe=inf"); show("screener nan", r, 80)
r=c.get("/api/accounts/..%2f..%2fetc/positions"); show("trav", r)
r=c.get("/api/accounts/%00/positions"); show("nul", r)
r=c.get(f"/api/accounts/{aid}/export/..%2F..%2Fx.csv"); show("exp", r)
r=c.get(f"/api/accounts/{aid}/export/fills.csv"); print(dict(r.headers))
r=c.get(f"/api/accounts/{aid}/statements/2026-11/html"); print(dict(r.headers)); 
r=c.get(f"/api/accounts/{aid}/statements/2026-1١/html"); show("month unicode", r)
r=c.get(f"/api/accounts/{aid}/statements/-001-11"); show("month neg", r)
r=c.get("/assets/..%2f..%2f..%2fpackage.json"); show("static trav", r, 80)
r=c.get("/assets/%2e%2e/index.html"); show("static trav2", r, 80)
r=c.get("/..%2f..%2f..%2f..%2fetc/passwd"); show("catchall", r, 60)
r=c.get("/api/nonexistent"); show("api404", r)
r=c.get("/x", headers={}); print("ui hdr", r.headers.get("cache-control"), r.headers.get("content-security-policy")[:40])
r=c.get("/assets/index-CvCQrK5e.js"); print("asset hdr", r.headers.get("cache-control"), r.headers.get("content-type"), r.headers.get("content-security-policy") is not None)
