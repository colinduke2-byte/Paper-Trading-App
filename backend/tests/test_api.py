import time

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.exchange import Exchange
from app.marketdata.synthetic import SyntheticFeed


def client(run_loop=False, **kw):
    now = time.time()
    ex = Exchange(SyntheticFeed(seed=3, now=now))
    ex.create_account("Margin", "margin")
    ex.tick()
    return TestClient(create_app(ex, run_loop=run_loop, **kw)), ex


def test_market_endpoints():
    c, ex = client()
    assert c.get("/api/status").json()["paper"] is True
    syms = c.get("/api/symbols", params={"q": "nv"}).json()
    assert syms[0]["symbol"] == "NVLX"
    q = c.get("/api/quotes", params={"symbols": "NVLX,ORBT,ZZZZ"}).json()
    assert set(q) == {"NVLX", "ORBT"} and q["NVLX"]["bid"] < q["NVLX"]["ask"]
    for tf in ("1m", "5m", "1h", "1d", "1w"):
        bars = c.get("/api/bars/NVLX", params={"tf": tf}).json()
        assert bars and all(b["low"] <= b["open"] <= b["high"] and b["low"] <= b["close"] <= b["high"] for b in bars)
        assert [b["time"] for b in bars] == sorted({b["time"] for b in bars})
    assert c.get("/api/bars/NVLX", params={"tf": "7m"}).status_code == 400
    assert c.get("/api/bars/ZZZZ").status_code == 404
    d = c.get("/api/depth/NVLX").json()
    assert d["simulated"] and len(d["bids"]) == 10 and d["bids"][0]["price"] < d["asks"][0]["price"]
    f = c.get("/api/fundamentals/NVLX").json()
    assert f["market_cap"] > 0 and f["high_52w"] >= f["low_52w"]
    assert len(c.get("/api/movers").json()["gainers"]) == 5
    tech = c.get("/api/screener", params={"sector": "Technology", "min_price": 60}).json()
    assert tech and all(r["sector"] == "Technology" and r["last"] >= 60 for r in tech)


def test_option_chain_endpoint():
    c, _ = client()
    exps = c.get("/api/options/expirations/NVLX").json()
    assert len(exps) >= 5
    ch = c.get("/api/options/chain/NVLX", params={"expiry": exps[1]["date"]}).json()
    assert ch["simulated"] and len(ch["rows"]) > 10
    assert c.get("/api/options/chain/NVLX", params={"expiry": "2020-01-01"}).status_code == 404
    assert c.get("/api/options/chain/NVLX", params={"expiry": "soon"}).status_code == 400


def test_trading_flow():
    c, ex = client()
    aid = c.get("/api/accounts").json()[0]["id"]
    pre = c.post(f"/api/accounts/{aid}/orders/preview", json={"symbol": "NVLX", "side": "buy", "qty": 10}).json()
    assert pre["ok"] and pre["estimated_total"] > 0 and pre["buying_power_after"] < pre["buying_power_before"]
    r = c.post(f"/api/accounts/{aid}/orders", json={"symbol": "NVLX", "side": "buy", "qty": 10})
    assert r.status_code == 200 and r.json()[0]["status"] == "filled"
    assert c.get(f"/api/accounts/{aid}/positions").json()[0]["qty"] == 10
    r = c.post(f"/api/accounts/{aid}/orders", json={"symbol": "NVLX", "side": "buy", "qty": 10_000_000})
    assert r.status_code == 422 and r.json()["rejected"]
    r = c.post(f"/api/accounts/{aid}/orders", json={"symbol": "NVLX", "side": "sell", "qty": 10, "type": "limit", "limit_price": 9999})
    oid = r.json()[0]["id"]
    assert len(c.get(f"/api/accounts/{aid}/orders", params={"status": "open"}).json()) == 1
    assert c.patch(f"/api/accounts/{aid}/orders/{oid}", json={"limit_price": 9998}).json()["limit_price"] == 9998
    assert c.delete(f"/api/accounts/{aid}/orders/{oid}").json()["status"] == "canceled"
    assert c.delete(f"/api/accounts/{aid}/orders/{oid}").status_code == 400
    assert c.delete(f"/api/accounts/{aid}/orders/NOPE").status_code == 404
    assert c.get("/api/accounts/NOPE").status_code == 404
    assert len(c.get(f"/api/accounts/{aid}/fills").json()) == 1
    assert len(c.get(f"/api/accounts/{aid}/ledger").json()) == 2
    # Trade statistics come from /analytics only (the old /performance and /closed routes were removed).
    assert c.get(f"/api/accounts/{aid}/analytics").json()["trades"]["overall"]["trades"] == 0
    assert c.get(f"/api/accounts/{aid}/performance").status_code == 404
    new = c.post("/api/accounts", json={"name": "Second", "type": "cash", "starting_cash": 5000}).json()
    assert new["cash"] == 5000 and c.post(f"/api/accounts/{new['id']}/reset", json={}).json()["cash"] == 5000
    assert c.post("/api/accounts", json={"type": "gold"}).status_code == 400
    assert c.put("/api/watchlists/Main", json=["nvlx", "ZZZZ", "ORBT"]).json() == {"Main": ["NVLX", "ORBT"]}
    al = c.post("/api/alerts", json={"symbol": "NVLX", "op": "above", "price": 1}).json()
    assert c.delete(f"/api/alerts/{al['id']}").json()["ok"] and c.get("/api/alerts").json() == []


def test_websocket_stream():
    c, ex = client(run_loop=True, tick_seconds=0.02)
    aid = list(ex.accounts)[0]
    with c:
        with c.websocket_connect("/ws") as ws:
            ws.send_json({"symbols": ["NVLX"], "account": aid})
            for _ in range(50):
                msg = ws.receive_json()
                if msg["quotes"]:
                    break
            assert msg["type"] == "tick" and "NVLX" in msg["quotes"] and msg["account"]["id"] == aid
