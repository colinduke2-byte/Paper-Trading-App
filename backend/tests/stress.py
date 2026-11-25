"""Shared pieces of the concurrency tests: a server that ticks at full speed, request workers and
the invariants every state (in memory or loaded from state.json) must satisfy.

Scale: the default is small and fast. Set PAPERDESK_STRESS=1 (or a larger number) to multiply the
number of requests per worker for a long soak run.
"""
import json
import os
import random
import shutil
import sys
import threading
from collections import Counter
from decimal import Decimal

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.core import calendar as cal
from app.core.clock import SimClock
from app.core.models import CANCELED, FILLED
from app.exchange import Exchange
from app.ledger.ledger import CAPITAL, CASH, FEES, REALIZED, position_account
from app.marketdata.feed import SimFeed
from app.marketdata.synthetic import SyntheticFeed
from app.options import chain
from app.options.symbology import build

D = Decimal
T0 = 1795500000.0  # 2026-11-23 11:20 UTC, a Monday
SCALE = int(os.environ.get("PAPERDESK_STRESS", "0") or 0)
OK_STATUSES = {200, 400, 404, 409, 422}  # anything else (500, 503) is a failure


def scaled(fast: int, long: int) -> int:
    return long * SCALE if SCALE else fast


# ---- invariants ---------------------------------------------------------------------------------
def problems(ex: Exchange) -> list:
    """Everything that is wrong with the books of `ex`; an empty list when they are consistent."""
    out = []
    fill_ids = Counter()
    for a in ex.accounts.values():
        led = a.ledger
        for t in led.transactions:
            if sum((v for _, v in t.postings), D(0)) != 0:
                out.append(f"{a.id}: transaction {t.id} does not balance")
        nonzero = lambda d: {k: v for k, v in d.items() if v}
        if nonzero(led.recompute()) != nonzero(led.balances):
            out.append(f"{a.id}: ledger balances differ from the sum of the transactions")
        if sum(led.balances.values(), D(0)) != 0:
            out.append(f"{a.id}: ledger does not sum to zero")
        symbols = set(a.lots) | {k[4:] for k in led.balances if k.startswith("pos:")}
        basis = D(0)
        for sym in symbols:
            if led.balance(position_account(sym)) != a.cost_basis(sym):
                out.append(f"{a.id}: position account of {sym} is {led.balance(position_account(sym))} "
                           f"but the lots cost {a.cost_basis(sym)}")
            basis += a.cost_basis(sym)
        # The identity of tests/test_ledger.py, with lifecycle postings (interest, dividends) allowed for.
        other = sum((v for k, v in led.balances.items()
                     if k not in (CASH, CAPITAL, REALIZED, FEES) and not k.startswith("pos:")), D(0))
        if a.cash != -led.balance(CAPITAL) + a.realized - a.fees_paid - basis - other:
            out.append(f"{a.id}: cash {a.cash} is not capital + realised - fees - cost basis")
        refs = Counter(t.ref for t in led.transactions if t.ref)
        for f in a.fills:
            fill_ids[f.id] += 1
            if refs[f.id] != 1:
                out.append(f"{a.id}: fill {f.id} has {refs[f.id]} ledger transactions")
        by_order = {}
        for f in a.fills:
            if f.order_id:
                by_order.setdefault(f.order_id, []).append(f)
        for oid, fs in by_order.items():
            o = ex.engine.orders.get(oid)
            if o is None:
                out.append(f"{a.id}: fills {[f.id for f in fs]} belong to order {oid}, which does not exist")
            elif o.account_id != a.id:
                out.append(f"{a.id}: order {oid} belongs to {o.account_id} but its fills are here")
    for fid, n in fill_ids.items():
        if n != 1:
            out.append(f"fill id {fid} is used {n} times")
    for oid, o in ex.engine.orders.items():
        a = ex.accounts.get(o.account_id)
        if o.id != oid:
            out.append(f"order stored as {oid} says it is {o.id}")
        if a is None:
            continue
        fs = [f for f in a.fills if f.order_id == oid]
        if o.legs:
            want = sorted((l["symbol"], o.qty * l["ratio"]) for l in o.legs) if o.status == FILLED else []
            if sorted((f.symbol, f.qty) for f in fs) != want:
                out.append(f"multi-leg order {oid} ({o.status}) has fills {[(f.symbol, str(f.qty)) for f in fs]}")
        elif sum((f.qty for f in fs), D(0)) != o.filled_qty:
            out.append(f"order {oid} ({o.status}) says {o.filled_qty} filled but its fills add up to "
                       f"{sum((f.qty for f in fs), D(0))}")
        if o.status == FILLED and o.filled_qty != o.qty:
            out.append(f"order {oid} is filled with {o.filled_qty} of {o.qty}")
        if o.status == CANCELED and not o.legs and o.filled_qty >= o.qty and o.qty > 0:
            out.append(f"order {oid} is canceled although all {o.qty} filled")
        if o.filled_qty > o.qty:
            out.append(f"order {oid} filled {o.filled_qty} of {o.qty}")
    return out


def load_state(path: str, scratch: str) -> Exchange:
    """A fresh exchange loaded from one state.json (copied first, so the server can keep replacing it)."""
    os.makedirs(scratch, exist_ok=True)
    shutil.copyfile(path, os.path.join(scratch, "state.json"))
    ex = Exchange(SimFeed(), SimClock(T0), data_dir=scratch)
    if not ex.load():
        raise ValueError("state.json could not be loaded")
    return ex


def file_problems(path: str, scratch: str) -> list:
    """Problems of the state saved on disk, including ids a restart would hand out twice."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        ex = load_state(path, scratch)
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001 - any failure to load is the finding
        return [f"state.json does not load: {e!r}"]
    out = problems(ex)
    number = lambda s: int("".join(ch for ch in s if ch.isdigit()))
    orders = [number(o["id"]) for o in raw["orders"]]
    fills = [number(f["id"]) for a in raw["accounts"] for f in a["fills"] if f["id"].startswith("F")]
    if orders and raw["seq"]["order"] <= max(orders):
        out.append(f"saved order counter {raw['seq']['order']} would reuse order id {max(orders)}")
    if fills and raw["seq"]["fill"] <= max(fills):
        out.append(f"saved fill counter {raw['seq']['fill']} would reuse fill id {max(fills)}")
    return out


# ---- the server under load ------------------------------------------------------------------------
class Rig:
    """A server whose loop ticks as fast as it can; every tick moves the simulated clock `step` seconds."""

    def __init__(self, tmp_path, seed: int = 7, step: float = 5.0, feed=None, accounts=(("margin", "1000000"),),
                 **app_kw):
        self.tmp = str(tmp_path)
        self.data = os.path.join(self.tmp, "data")
        self.clock = SimClock(T0)
        self.feed = feed or SyntheticFeed(seed=seed, now=T0)
        self.ex = ex = Exchange(self.feed, self.clock, data_dir=self.data)
        self.accounts = [ex.create_account(f"Account {i}", kind, D(cash)).id for i, (kind, cash) in enumerate(accounts)]
        ex.tick()
        self.ticks = 0
        tick = ex.tick

        def fast_tick():
            self.clock.advance(step)
            self.ticks += 1
            return tick()

        ex.tick = fast_tick
        app_kw.setdefault("tick_seconds", 0.005)
        app_kw.setdefault("replay_tick_seconds", 0.005)
        self.app = create_app(ex, run_loop=True, **app_kw)
        self.ctx = self.app.state.ctx
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.failures = []  # (method, path, status, body) of every response that is not an expected status
        self.lock = threading.Lock()
        self.accepted = []  # [(client_id or None, [order ids])] of every accepted submit to accounts[0]
        self.canceled = []  # order ids whose cancel returned 200
        self.busy = 0  # 503 answers (soak runs only)
        self.open_ids = []
        self._interval = sys.getswitchinterval()

    def __enter__(self):
        sys.setswitchinterval(1e-5)  # switch threads often, so races that exist show up
        self.client.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            return self.client.__exit__(*exc)
        finally:
            sys.setswitchinterval(self._interval)

    def call(self, method: str, path: str, **kw):
        r = self.client.request(method, path, **kw)
        if r.status_code == 503 and SCALE:
            # The soak ticks 200 times as often as a real server on a state of thousands of orders, so the
            # tick holds the lock most of the time and a request can wait out its 2 seconds. "Busy" is the
            # designed answer then; the soak checks the books, the fast run also forbids any 503.
            with self.lock:
                self.busy += 1
            return r
        if r.status_code not in OK_STATUSES:
            with self.lock:
                self.failures.append((method, path, r.status_code, r.text[:300]))
        return r

    def price(self, symbol: str) -> Decimal:
        return self.feed.quotes[symbol].last

    def run(self, workers: list, timeout: float = 600) -> None:
        errors = []

        def guard(fn):
            def go():
                try:
                    fn()
                except BaseException as e:  # noqa: BLE001 - reported below
                    errors.append(repr(e))
            return go

        threads = [threading.Thread(target=guard(w), daemon=True) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout)
        assert not any(t.is_alive() for t in threads), "a worker is stuck"
        assert not errors, errors

    def watch_state_file(self, stop: threading.Event, found: list, scratch: str) -> None:
        """Until `stop`: load state.json over and over and collect whatever is inconsistent in it."""
        path = os.path.join(self.data, "state.json")
        n = 0
        while not stop.is_set():
            n += 1
            for p in file_problems(path, os.path.join(self.tmp, scratch)):
                found.append(p)
            if len(found) > 20:
                return
            stop.wait(0.02)
        found.extend([] if n else ["the state file was never checked"])


# ---- request mix -------------------------------------------------------------------------------------
def trader(rig: Rig, seed: int, n: int, steady: str, chaos: str = None):
    """One worker: `n` requests drawn from a seeded mix of everything the API can change."""
    rng = random.Random(seed)
    symbols = sorted(rig.feed.info)[:6]
    expiry = chain.expirations(cal.ny_date(T0))[3]

    def submit(aid, body):
        r = rig.call("POST", f"/api/accounts/{aid}/orders", json=body)
        if r.status_code == 200 and aid == steady:
            ids = [o["id"] for o in r.json()]
            with rig.lock:
                rig.accepted.append((body.get("client_id"), ids))
                rig.open_ids.extend(ids)
        return r

    def pick_order():
        with rig.lock:
            return rng.choice(rig.open_ids) if rig.open_ids else "O00000000"

    def market(aid):
        submit(aid, {"symbol": rng.choice(symbols), "side": rng.choice(["buy", "buy", "sell"]), "qty": rng.randint(1, 20)})

    def resting(aid):
        s = rng.choice(symbols)
        submit(aid, {"symbol": s, "side": "buy", "qty": rng.randint(1, 10), "type": "limit", "tif": "gtc",
                     "limit_price": str((rig.price(s) * D("0.5")).quantize(D("0.01")))})

    def near(aid):
        s, side = rng.choice(symbols), rng.choice(["buy", "sell"])
        px = rig.price(s) * (D("0.999") if side == "buy" else D("1.001"))
        submit(aid, {"symbol": s, "side": side, "qty": rng.randint(1, 10), "type": "limit", "tif": "gtc",
                     "limit_price": str(px.quantize(D("0.01")))})

    def bracket(aid):
        s = rng.choice(symbols)
        px = rig.price(s)
        submit(aid, {"symbol": s, "side": "buy", "qty": rng.randint(1, 5), "take_profit": str((px * D("1.002")).quantize(D("0.01"))),
                     "stop_loss": str((px * D("0.998")).quantize(D("0.01")))})

    def oco(aid):
        s = rng.choice(symbols)
        px = rig.price(s)
        submit(aid, {"oco": [
            {"symbol": s, "side": "sell", "qty": 1, "type": "limit", "tif": "gtc", "limit_price": str((px * D("1.001")).quantize(D("0.01")))},
            {"symbol": s, "side": "sell", "qty": 1, "type": "stop", "tif": "gtc", "stop_price": str((px * D("0.999")).quantize(D("0.01")))}]})

    def multileg(aid):
        s = rng.choice(symbols)
        strike = (rig.price(s) / 5).quantize(D(1)) * 5
        legs = [{"symbol": build(s, expiry, "C", strike), "side": "buy", "ratio": 1},
                {"symbol": build(s, expiry, "C", strike + 5), "side": "sell", "ratio": 1}]
        if rng.random() < 0.3:
            legs = [dict(l, side="sell" if l["side"] == "buy" else "buy") for l in legs]
        r = rig.call("POST", f"/api/accounts/{aid}/multileg", json={"legs": legs, "qty": rng.randint(1, 3)})
        if r.status_code == 200 and aid == steady:
            with rig.lock:
                rig.accepted.append((None, [r.json()["id"]]))

    def cancel(aid):
        oid = pick_order()
        r = rig.call("DELETE", f"/api/accounts/{steady}/orders/{oid}")
        if r.status_code == 200:
            with rig.lock:
                rig.canceled.append(oid)

    def replace(aid):
        s = rng.choice(symbols)
        body = rng.choice([{"limit_price": str((rig.price(s) * D("0.6")).quantize(D("0.01")))}, {"qty": rng.randint(1, 30)},
                           {"qty": rng.randint(1, 30), "stop_price": "1.00"}])
        rig.call("PATCH", f"/api/accounts/{steady}/orders/{pick_order()}", json=body)

    def cancel_all(aid):
        if rng.random() < 0.2:
            rig.call("DELETE", f"/api/accounts/{aid}/orders")

    def double_click(aid):
        s, aid = rng.choice(symbols), steady  # an account that is never reset, so the first order still exists
        body = {"symbol": s, "side": "buy", "qty": 1, "client_id": f"w{seed}-{rng.random()}"}
        a, b = submit(aid, body), submit(aid, body)
        if a.status_code == b.status_code == 200 and a.json()[0]["id"] != b.json()[0]["id"]:
            with rig.lock:
                rig.failures.append(("POST", "orders", 200, f"client id {body['client_id']} made two orders"))

    def reset(aid):
        if chaos:
            rig.call("POST", f"/api/accounts/{chaos}/reset", json={})

    def create_delete(aid):
        r = rig.call("POST", "/api/accounts", json={"name": "temp", "type": rng.choice(["cash", "margin"]), "starting_cash": 50000})
        if r.status_code == 200:
            new = r.json()["id"]
            submit(new, {"symbol": rng.choice(symbols), "side": "buy", "qty": 1})
            rig.call("DELETE", f"/api/accounts/{new}")

    def settings(aid):
        which = rng.randint(0, 3)
        if which == 0:
            rig.call("PUT", f"/api/accounts/{aid}/settings", json={"settings": {"margin_interest_rate": str(rng.randint(1, 9))}})
        elif which == 1:
            rig.call("POST", f"/api/accounts/{aid}/settings/reset")
        else:
            rig.call("PUT", f"/api/accounts/{aid}/lot-method", json={"method": rng.choice(["fifo", "lifo", "highest_cost"])})

    def lists(aid):
        name = f"list{rng.randint(0, 3)}"
        if rng.random() < 0.6:
            rig.call("PUT", f"/api/watchlists/{name}", json=rng.sample(symbols, 3))
        else:
            rig.call("DELETE", f"/api/watchlists/{name}")
        r = rig.call("POST", "/api/alerts", json={"symbol": rng.choice(symbols), "op": "above", "price": "1"})
        if r.status_code == 200 and rng.random() < 0.7:
            rig.call("DELETE", f"/api/alerts/{r.json()['id']}")
        rig.call("GET", "/api/notifications", params={"account_id": aid})
        if rng.random() < 0.3:
            rig.call("POST", "/api/notifications/read", json={"account_id": aid})
        r = rig.call("POST", "/api/alert-rules", json={"kind": "price", "symbol": rng.choice(symbols), "op": "above",
                                                      "price": str(rig.price(symbols[0]))})
        if r.status_code == 200 and rng.random() < 0.5:
            rig.call("DELETE", f"/api/alert-rules/{r.json()['id']}")

    def journal(aid):
        r = rig.call("POST", f"/api/accounts/{steady}/journal", json={"target_type": "order", "target_id": pick_order(), "note": "n"})
        if r.status_code == 200 and rng.random() < 0.5:
            rig.call("DELETE", f"/api/accounts/{steady}/journal/{r.json()['id']}")

    def read(aid):
        path = rng.choice(["", "/positions", "/orders", "/fills", "/ledger", "/performance", "/analytics", "/lifecycle",
                           "/closed", "/gains", "/journal", "/settings", "/export/fills", "/export/ledger", "/statements"])
        rig.call("GET", f"/api/accounts/{aid}{path}")
        rig.call("GET", rng.choice(["/api/accounts", "/api/status", "/api/watchlists", "/api/alerts", "/api/movers"]))

    mix = [(market, 16), (resting, 6), (near, 10), (bracket, 4), (oco, 3), (multileg, 6), (cancel, 10), (replace, 6),
           (cancel_all, 1), (double_click, 3), (reset, 3), (create_delete, 2), (settings, 3), (lists, 3), (journal, 2),
           (read, 12)]
    ops, weights = [m[0] for m in mix], [m[1] for m in mix]

    def work():
        for _ in range(n):
            op = rng.choices(ops, weights)[0]
            op(chaos if chaos and rng.random() < 0.25 else steady)

    return work
