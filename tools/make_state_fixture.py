"""Run an old checkout's own code to write a state directory. Usage: genfixture.py <backend dir> <out dir>"""
import os, sys, json
backend, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, backend)
os.chdir(backend)
os.environ["PAPERDESK_DATA"] = out
os.environ["PAPERDESK_FEED"] = "synthetic"
os.environ["PAPERDESK_SESSIONS"] = "always"
for k in list(os.environ):
    if k.startswith("APCA_") or k.startswith("PAPERDESK_LAN") or k == "PAPERDESK_PASSCODE":
        del os.environ[k]
from fastapi.testclient import TestClient
from app.api import server
app = server.create_app(run_loop=False)
ex = app.state.exchange
c = TestClient(app)
accts = c.get("/api/accounts").json()
margin = [a for a in accts if a["type"] == "margin"][0]["id"]
cash = [a for a in accts if a["type"] == "cash"][0]["id"]
def tick(n=3):
    for _ in range(n):
        ex.clock.advance(1) if hasattr(ex.clock, "advance") else None
        ex.tick()
def order(aid, **o):
    r = c.post(f"/api/accounts/{aid}/orders", json=o)
    print(aid, o.get("symbol"), o.get("side"), r.status_code, r.text[:120])
    tick()
order(margin, symbol="MKTX", side="buy", qty=10, type="market", tif="day")
order(margin, symbol="TECX", side="sell", qty=5, type="market", tif="day")   # short
order(margin, symbol="MKTX", side="sell", qty=4, type="market", tif="day")   # realise some
order(margin, symbol="NVLX", side="buy", qty=3, type="limit", limit_price=1, tif="gtc")  # stays working
exp = c.get("/api/options/expirations/MKTX").json()
if exp:
    ch = c.get("/api/options/chain/MKTX", params={"expiry": exp[1]["date"] if len(exp) > 1 else exp[0]["date"]}).json()
    rows = ch["rows"]; row = rows[len(rows)//2]
    call = row["call"]["symbol"] if isinstance(row.get("call"), dict) else row.get("call_symbol")
    order(margin, symbol=call, side="buy", qty=2, type="market", tif="day")
order(cash, symbol="ORBT", side="buy", qty=7, type="market", tif="day")
order(cash, symbol="ORBT", side="sell", qty=2, type="market", tif="day")
c.put("/api/watchlists/Tech", json=["TECX", "NVLX"])
c.post("/api/alerts", json={"symbol": "MKTX", "op": ">", "price": 100000})
tick(5)
ex.save(force=True)
d = json.load(open(os.path.join(out, "state.json")))
print("written", out, "version", d.get("version"), "accounts", len(d["accounts"]), "orders", len(d["orders"]), sorted(d))
