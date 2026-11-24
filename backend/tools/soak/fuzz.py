"""Schema-free fuzzing of every non-GET /api route through FastAPI's TestClient.

    report = fuzz(seed=1, requests=3000)        # {"server_errors": [...], "statuses": Counter, ...}

Bodies are random JSON values (wrong types, huge and tiny numbers, NaN and Infinity as text,
deep nesting, long and odd unicode strings, missing and extra fields) and mutations of a valid
body for the route, where one field at a time gets an adversarial value. Path parameters are real
ids (accounts, orders, journal entries, months) or junk. A server error is any 5xx answer or an
exception escaping the app; each is reported with the request and the app frame it came from.
After the run the exchange's invariants are checked, and the state must save and load.
"""
import json
import random
import traceback
from decimal import Decimal
from collections import Counter

from fastapi.routing import APIRoute

WEIRD_STRINGS = ["", " ", "0", "-0", "1e999999", "-1e-999999", "NaN", "nan", "Infinity", "-Infinity", "sNaN",
                 "１２３", "٣", "1_000", " 5 ", "0x10", "1,000", "12.345678901234567890", "9" * 400,
                 "buy", "sell", "market", "limit", "gtd", "credit", "debit", "fifo", "A0001", "O00000001",
                 "../../etc/passwd", "<script>alert(1)</script>", "\u0000", "\ud800", "\U0001F600" * 50,
                 "é" * 5000, "x" * 70000, "2026-02-30", "2026-13-01", "9999-12-31", "0001-01-01", "2026-11",
                 "true", "null"]
WEIRD_NUMBERS = [0, -1, 1, 2 ** 31, 2 ** 63, 10 ** 30, 10 ** 400, -10 ** 400, 0.1, 1e-300, 1e308, -1e308,
                 1.5, 0.00001, 123456789.123456789, 1e15 + 0.3]
RAW_BODIES = [b"", b"{", b"[", b"null", b"NaN", b"Infinity", b'{"qty": NaN}', b'{"qty": Infinity}',
              b'{"qty": -Infinity}', b'{"symbol": "NVLX", "side": "buy", "qty": 1e999}', b"\xff\xfe",
              b'"just a string"', b"12", b"[" * 5000 + b"]" * 5000, b'{"a":' * 3000 + b"1" + b"}" * 3000,
              b'{"legs": ' + b"[" * 2000 + b"]" * 2000 + b"}", b'{"symbol": "\\ud800", "side": "buy", "qty": 1}']

KEYS = ["symbol", "side", "qty", "type", "tif", "limit_price", "stop_price", "trail_amount", "trail_percent",
        "take_profit", "stop_loss", "good_till_date", "extended_hours", "client_id", "legs", "oco", "triggers",
        "triggers_oco", "ratio", "price_effect", "name", "starting_cash", "method", "settings", "note", "tags",
        "target_type", "target_id", "op", "price", "session", "speed", "seconds", "time", "offset", "play",
        "account_type", "passcode", "margin_interest_rate", "dividend_reinvestment", "margin_call_grace_hours"]


def _valid_bodies(ex, aid: str) -> dict:
    """A plausible body for each route (by path pattern), to mutate one field at a time."""
    from app.core import calendar as cal
    from app.options import chain as optchain
    from app.options.symbology import build
    q = ex.feed.quote("NVLX")
    last = q.last if q else 100
    exp = optchain.expirations(cal.ny_date(ex.now()))[1]
    step = optchain.strike_step(float(last))
    atm = (last / step).to_integral_value() * step
    c1, c2 = build("NVLX", exp, "C", atm), build("NVLX", exp, "C", atm + step)
    order = {"symbol": "NVLX", "side": "buy", "qty": 2, "type": "limit", "limit_price": str((last * Decimal("0.98")).quantize(Decimal("0.01"))),
             "tif": "gtc"}
    legs = {"legs": [{"symbol": c1, "side": "buy", "ratio": 1}, {"symbol": c2, "side": "sell", "ratio": 1}], "qty": 1}
    return {
        "/api/accounts": {"name": "Fuzz", "type": "margin", "starting_cash": 50000},
        "/api/accounts/{aid}/reset": {"starting_cash": 75000},
        "/api/accounts/{aid}/orders": order, "/api/accounts/{aid}/orders/preview": order,
        "/api/accounts/{aid}/orders/{oid}": {"qty": 3, "limit_price": str(last)},
        "/api/accounts/{aid}/multileg": legs, "/api/accounts/{aid}/multileg/preview": legs,
        "/api/accounts/{aid}/settings": {"margin_interest_rate": "7", "dividend_reinvestment": True},
        "/api/accounts/{aid}/lot-method": {"method": "lifo"},
        "/api/accounts/{aid}/journal": {"target_type": "order", "target_id": "O00000001", "note": "n", "tags": ["a"]},
        "/api/accounts/{aid}/journal/{jid}": {"note": "edited", "tags": "x,y"},
        "/api/alerts": {"symbol": "NVLX", "op": "above", "price": str(last)},
        "/api/watchlists/{name}": ["NVLX", "ORBT"],
        "/api/replay/start": {"session": "sample-2026-03-13", "starting_cash": 50000, "account_type": "cash"},
        "/api/replay/speed": {"speed": 5}, "/api/replay/step": {"seconds": 60}, "/api/replay/seek": {"offset": 600},
        "/api/auth/login": {"passcode": "wrong passcode"},
    }


class Fuzzer:
    def __init__(self, ex, seed: int = 0, max_depth: int = 4):
        from fastapi.testclient import TestClient
        from app.api.server import create_app
        self.ex = ex
        self.app = create_app(ex, run_loop=False)
        self.client = TestClient(self.app, raise_server_exceptions=True)
        self.rng = random.Random(f"fuzz-{seed}")
        self.max_depth = max_depth
        self.routes = sorted({(m, r.path) for r in self.app.routes if isinstance(r, APIRoute)
                              for m in r.methods if m != "GET" and r.path.startswith("/api")})
        self.errors: list = []
        self.statuses: Counter = Counter()

    # ---- values --------------------------------------------------------------------------
    def value(self, depth: int = 0):
        r = self.rng
        x = r.random()
        if depth >= self.max_depth or x < 0.35:
            return r.choice(WEIRD_STRINGS) if r.random() < 0.5 else r.choice(WEIRD_NUMBERS + [None, True, False])
        if x < 0.65:
            return [self.value(depth + 1) for _ in range(r.randint(0, 4))]
        return {r.choice(KEYS) if r.random() < 0.8 else r.choice(WEIRD_STRINGS): self.value(depth + 1)
                for _ in range(r.randint(0, 6))}

    def body(self, path: str):
        r = self.rng
        valid = _valid_bodies(self.ex, "A0001").get(path)
        x = r.random()
        if x < 0.1:
            return ("raw", r.choice(RAW_BODIES))
        if valid is not None and x < 0.75:
            b = json.loads(json.dumps(valid))
            if isinstance(b, dict):
                for _ in range(r.randint(1, 2)):
                    k = r.choice(list(b) + KEYS)
                    if r.random() < 0.15:
                        b.pop(k, None)
                    else:
                        b[k] = self.value(1)
                if "legs" in b and isinstance(b["legs"], list) and b["legs"] and r.random() < 0.4:
                    leg = r.choice(b["legs"])
                    if isinstance(leg, dict):
                        leg[r.choice(("symbol", "side", "ratio"))] = self.value(2)
                if "oco" in b and isinstance(b["oco"], list) and b["oco"] and r.random() < 0.4:
                    b["oco"][0] = self.value(1)
            else:
                b = self.value(0) if r.random() < 0.5 else b + [self.value(1)]
            return ("json", b)
        return ("json", self.value(0))

    def path_value(self, name: str) -> str:
        r = self.rng
        ex = self.ex
        if r.random() < 0.8:
            if name == "aid" and ex.accounts:
                return r.choice(sorted(ex.accounts))
            if name == "oid" and ex.engine.orders:
                return r.choice(sorted(ex.engine.orders))
            if name == "jid":
                return r.choice(("J1", "J2", "J3"))
            if name == "name":
                return r.choice(("Main", "Tech", "x" * 60))
            if name == "alert_id" and ex.alerts:
                return r.choice([a["id"] for a in ex.alerts])
        return r.choice(("A9999", "%00", "..", "ü", "O" + "9" * 30, "%F0%9F%98%80", "x" * 300, "J0", "-1"))

    # ---- requests -------------------------------------------------------------------------
    def request(self, method: str, path: str) -> None:
        url = path
        for name in ("aid", "oid", "jid", "name", "alert_id", "kind", "month", "symbol"):
            if "{" + name + "}" in url:
                url = url.replace("{" + name + "}", self.path_value(name))
        kind, body = self.body(path)
        # JSON is encoded here with ASCII escapes, as a browser would send it (\ud800 travels as an escape)
        content = body if kind == "raw" else json.dumps(body).encode()
        kw = {"content": content, "headers": {"content-type": "application/json"}}
        try:
            r = self.client.request(method, url, **kw)
            self.statuses[r.status_code] += 1
            if r.status_code >= 500:
                self._record(method, url, kind, body, f"HTTP {r.status_code}: {r.text[:200]}", None)
        except Exception as e:  # escaped the app: the server would answer 500
            self.statuses["exception"] += 1
            self._record(method, url, kind, body, f"{type(e).__name__}: {str(e)[:200]}", e)

    def _record(self, method, url, kind, body, what, exc) -> None:
        where = ""
        if exc is not None:
            frames = [f for f in traceback.extract_tb(exc.__traceback__) if "/app/" in f.filename]
            if frames:
                f = frames[-1]
                where = f"{f.filename.split('/backend/')[-1]}:{f.lineno} ({f.name})"
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else json.dumps(body, ensure_ascii=True)
        self.errors.append({"method": method, "url": url[:200], "body": text[:500], "error": what, "where": where,
                            "signature": f"{method} {_route_of(url, self)} {what.split(':')[0]} {where}"})

    def run(self, n: int) -> dict:
        for _ in range(n):
            method, path = self.rng.choice(self.routes)
            self.request(method, path)
        if self.app.state.ctx.replay is not None:
            self.client.post("/api/replay/exit")
        return {"routes": self.routes, "statuses": dict(self.statuses), "server_errors": self.errors}


def _route_of(url: str, fz: "Fuzzer") -> str:
    parts = url.split("?")[0].strip("/").split("/")
    for m, p in fz.routes:
        pp = p.strip("/").split("/")
        if len(pp) == len(parts) and all(a == b or a.startswith("{") for a, b in zip(pp, parts)):
            return p
    return url


def fuzz(ex, seed: int = 0, requests: int = 2000) -> dict:
    return Fuzzer(ex, seed).run(requests)
