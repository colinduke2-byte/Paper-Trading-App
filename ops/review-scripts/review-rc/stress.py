import sys, json, threading, time, urllib.request, collections
base = sys.argv[1]; stop = time.time() + 25
codes = collections.Counter(); errs = []
def call(method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r: codes[r.status] += 1; return json.loads(r.read())
    except urllib.error.HTTPError as e:
        codes[e.code] += 1
        if e.code >= 500: errs.append((method, path, e.read()[:200]))
    except Exception as e: codes[repr(e)[:40]] += 1
acct = call("POST", "/api/accounts", {"name": "stress", "type": "margin", "starting_cash": "1000000"})["id"]
syms = ["MKTX", "TECX", "NVLX", "ORBT", "PXLR", "AUTO", "FNBK", "ENRG"]
def worker(i):
    n = 0
    while time.time() < stop:
        s = syms[(i + n) % len(syms)]; n += 1
        r = call("POST", f"/api/accounts/{acct}/orders", {"symbol": s, "side": "buy" if n % 2 else "sell", "qty": 5, "type": "limit", "limit_price": "1" if n % 2 else "9999", "tif": "gtc", "take_profit": None})
        call("POST", f"/api/accounts/{acct}/orders", {"symbol": s, "side": "buy" if n % 3 else "sell", "qty": 3})
        if r and n % 2: call("DELETE", f"/api/accounts/{acct}/orders/{r[0]['id']}")
        if n % 25 == 0: call("DELETE", f"/api/accounts/{acct}/orders")
        call("GET", f"/api/accounts/{acct}/positions"); call("GET", f"/api/accounts/{acct}/analytics")
ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
[t.start() for t in ts]; [t.join() for t in ts]
print(dict(codes)); print(errs[:5])
s = call("GET", f"/api/accounts/{acct}"); print("equity", s["equity"], "cash", s["cash"])
call("DELETE", f"/api/accounts/{acct}")
