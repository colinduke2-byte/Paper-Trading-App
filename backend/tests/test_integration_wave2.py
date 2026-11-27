"""Interaction tests for integration/wave2: where the features merged in this wave meet.

Merged here: backup and storage (PR 19), tax lots (PR 13), crypto (PR 14), order ticket (PR 18), discovery
(PR 20), hardening (PR 30), cross-browser (PR 11), usability polish, on top of main's thread safety (PR 29).
Each test names the two (or three) features it puts together.
"""
import json
import os
import threading
import time

from fastapi.testclient import TestClient

from app.api import locking as api_locking
from app.api.server import create_app
from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata.synthetic import SyntheticFeed
from helpers import D, T0


def make_app(tmp=None, run_loop=False, **kw):
    ex = Exchange(SyntheticFeed(seed=3, now=T0), SimClock(T0), data_dir=str(tmp) if tmp else None)
    ex.create_account("Margin", "margin", D("1000000"))
    ex.tick()
    app = create_app(ex, run_loop=run_loop, **kw)
    return TestClient(app), app, ex


class Holder:
    """Holds a lock on another thread until released: a stuck request."""

    def __init__(self, lock):
        self.lock = lock
        self.taken, self.done = threading.Event(), threading.Event()

    def __enter__(self):
        def hold():
            with self.lock.hold(5, label="stuck request"):
                self.taken.set()
                self.done.wait(30)
        self.thread = threading.Thread(target=hold, daemon=True)
        self.thread.start()
        assert self.taken.wait(5)
        return self

    def __exit__(self, *exc):
        self.done.set()
        self.thread.join(5)


# ---- backup (PR 19) x thread safety (PR 29) ------------------------------------------------------------

def test_data_routes_answer_busy_and_never_block_the_event_loop_while_the_lock_is_stuck(monkeypatch, tmp_path):
    """The backup routes are @unlocked (they do file I/O) and take ctx.locked() themselves. A stuck lock must
    give 503 busy, not a hang, and the async upload routes must not wait for the lock on the event loop."""
    monkeypatch.setattr(api_locking, "ROUTE_WAIT", 0.05)
    c, app, ex = make_app(tmp_path)
    ctx = app.state.ctx
    aid = list(ex.accounts)[0]
    name = c.post("/api/data/backups").json()["name"]
    token = c.post("/api/data/restore/preview", json={"backup": name}).json()["token"]
    exported = c.get(f"/api/data/accounts/{aid}/export").content
    before = json.dumps([a.to_plain() for a in ex.accounts.values()], sort_keys=True)
    with Holder(ctx.lock):
        calls = [("POST", "/api/data/backups", {}), ("POST", "/api/data/restore/preview", {"json": {"backup": name}}),
                 ("POST", "/api/data/restore/apply", {"json": {"token": token}}), ("GET", "/api/data/export", {}),
                 ("GET", f"/api/data/accounts/{aid}/export", {}),
                 ("POST", "/api/data/accounts/import/preview",
                  {"content": exported, "headers": {"content-type": "application/json"}})]
        for method, url, kw in calls:
            r = c.request(method, url, **kw)
            assert r.status_code == 503 and r.json()["busy"] is True, f"{method} {url}: {r.status_code} {r.text[:200]}"
        # the event loop stays free while an upload route waits for the lock in its worker thread
        monkeypatch.setattr(api_locking, "ROUTE_WAIT", 1.5)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", c.post("/api/data/restore/apply", json={"token": token})))
        t.start()
        time.sleep(0.2)
        t0 = time.monotonic()
        assert c.get("/api/auth/state").status_code == 200
        assert time.monotonic() - t0 < 1.0, "the event loop waited for the exchange lock"
        t.join(5)
        assert out["r"].status_code == 503
    assert json.dumps([a.to_plain() for a in ex.accounts.values()], sort_keys=True) == before
    # and once the lock is free the same token restores
    assert c.post("/api/data/restore/apply", json={"token": token}).status_code == 200


# ---- tax lots (PR 13) x storage format v2 (PR 19) x rollback (PR 29) -----------------------------------

def _lots_view(ex, aid):
    a = ex.accounts[aid]
    return {s: [(str(l.qty), str(l.price), l.id) for l in ls] for s, ls in a.lots.items()}, a.lot_seq


def test_lot_ids_and_a_resting_lot_order_survive_a_segment_save_and_reload(tmp_path):
    """The v2 schema validator must accept the lot id field; before the fix every account holding a
    position loaded as damaged and the data folder went into recovery."""
    from helpers import make_exchange, move
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("t", "margin", D("100000")).id
    for _ in range(3):
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
        move(ex, feed, clock, "ABC", "100.00")
    ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": 4, "type": "limit", "limit_price": "500", "tif": "gtc",
                  "lots": [{"lot_id": "L2", "qty": 4}]})
    assert ex.save(force=True)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1})  # appended to the segments by the next save
    assert ex.save()
    back = Exchange(feed, clock, data_dir=str(tmp_path))
    assert back.load(), back.store.recovery
    assert _lots_view(back, a) == _lots_view(ex, a)
    [rest] = [o for o in back.engine.orders.values() if o.active]
    assert [(l["lot_id"], str(l["qty"])) for l in rest.lots] == [("L2", "4")]
    # the next lot after a reload gets a fresh id, never one in use
    back.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1})
    ids = [l.id for l in back.accounts[a].lots["ABC"]]
    assert len(ids) == len(set(ids))


def test_a_rolled_back_fill_does_not_use_up_a_lot_id(monkeypatch):
    from helpers import make_exchange
    import pytest
    ex, feed, clock = make_exchange()
    acct = ex.create_account("t", "margin", D("100000"))
    ex.submit(acct.id, {"symbol": "ABC", "side": "buy", "qty": 1})
    before = _lots_view(ex, acct.id)
    real = ex.lifecycle.on_fill

    def fault(*args, **kw):
        raise RuntimeError("fault after the fill was booked")
    monkeypatch.setattr(ex.lifecycle, "on_fill", fault)
    with pytest.raises(RuntimeError):
        ex.submit(acct.id, {"symbol": "ABC", "side": "buy", "qty": 1})
    monkeypatch.setattr(ex.lifecycle, "on_fill", real)
    assert _lots_view(ex, acct.id) == before
    ex.submit(acct.id, {"symbol": "ABC", "side": "buy", "qty": 1})
    assert [l.id for l in ex.accounts[acct.id].lots["ABC"]] == ["L1", "L2"]


# ---- tax lots (PR 13) x crypto (PR 14) x cash-account rules ---------------------------------------------

def _crypto(tmp=None, qinc="0.00001"):
    from test_crypto import COIN, SAT, coin_info, set_coin
    from app.core.clock import SimClock as _Clock
    from app.marketdata.feed import SimFeed
    clock = _Clock(SAT)
    feed = SimFeed({"ABC": {"symbol": "ABC", "name": "ABC", "sector": "Test", "vol": 0.3}, COIN: coin_info(qinc=qinc)})
    ex = Exchange(feed, clock, session_mode="us", data_dir=str(tmp) if tmp else None)
    feed.set_quote("ABC", D("99.99"), D("100.00"), D("100.00"))
    set_coin(feed, "40000.00")
    ex.tick()
    c = ex.create_account("Cash", "cash", D("10000")).id
    return ex, feed, clock, c, COIN


def _buy_lots(ex, feed, clock, c, coin, prices, qty="0.01234"):
    from test_crypto import set_coin
    for px in prices:
        clock.advance(60)
        set_coin(feed, px)
        ex.tick()
        ex.submit(c, {"symbol": coin, "side": "buy", "qty": qty})


def test_a_crypto_lot_can_be_named_with_the_coins_own_quantity_step():
    """Lots allowed at most 4 decimal places; crypto quantities go to the coin's increment (here 0.00001)."""
    ex, feed, clock, c, coin = _crypto()
    _buy_lots(ex, feed, clock, c, coin, ["40000.00", "42000.00", "38000.00"])
    a = ex.accounts[c]
    assert [l.id for l in a.lots[coin]] == ["L1", "L2", "L3"]
    [o] = ex.submit(c, {"symbol": coin, "side": "sell", "qty": "0.00617", "lots": [{"lot_id": "L2", "qty": "0.00617"}]})
    assert o.status == "filled"
    left = {l.id: l.qty for l in a.lots[coin]}
    assert left == {"L1": D("0.01234"), "L2": D("0.00617"), "L3": D("0.01234")}
    assert a.cash >= 0


def test_a_crypto_lot_order_respects_the_no_short_rule_with_other_resting_sells():
    import pytest
    from app.exchange import OrderRejected
    ex, feed, clock, c, coin = _crypto()
    _buy_lots(ex, feed, clock, c, coin, ["40000.00", "42000.00"])
    ex.submit(c, {"symbol": coin, "side": "sell", "qty": "0.02", "type": "limit", "limit_price": "90000.00", "tif": "gtc"})
    with pytest.raises(OrderRejected, match="cannot be sold short"):
        ex.submit(c, {"symbol": coin, "side": "sell", "qty": "0.01", "type": "limit", "limit_price": "90000.00",
                      "tif": "gtc", "lots": [{"lot_id": "L1", "qty": "0.01"}]})


def test_crypto_fractional_lots_and_fees_survive_a_segment_save_and_reload(tmp_path):
    ex, feed, clock, c, coin = _crypto(tmp_path)
    _buy_lots(ex, feed, clock, c, coin, ["40000.00", "41000.00"])
    ex.submit(c, {"symbol": coin, "side": "sell", "qty": "0.00123", "lots": [{"lot_id": "L1", "qty": "0.00123"}]})
    assert ex.save(force=True)
    back = Exchange(feed, clock, session_mode="us", data_dir=str(tmp_path))
    assert back.load(), back.store.recovery
    a, b = ex.accounts[c], back.accounts[c]
    assert _lots_view(back, c) == _lots_view(ex, c)
    assert b.cash == a.cash and sum(f.fee for f in b.fills) == sum(f.fee for f in a.fills) > 0


# ---- order ticket linked orders (PR 18) x tax lots (PR 13) ----------------------------------------------

def test_linked_orders_cannot_name_tax_lots_and_reserve_nothing():
    """The ticket drops the lot choice when a link is set; the server refuses it too, and nothing stays reserved."""
    import pytest
    from app.exchange import OrderRejected
    from helpers import make_exchange, move
    ex, feed, clock = make_exchange()
    a = ex.create_account("t", "margin", D("100000")).id
    for _ in range(2):
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
        move(ex, feed, clock, "ABC", "100.00")
    lot = {"lots": [{"lot_id": "L2", "qty": 5}]}
    sell = {"symbol": "ABC", "side": "sell", "qty": 5, "tif": "gtc"}
    with pytest.raises(OrderRejected, match="one-cancels-other"):
        ex.submit(a, {"oco": [dict(sell, type="limit", limit_price="200", **lot), dict(sell, type="stop", stop_price="50")]})
    with pytest.raises(OrderRejected, match="one-triggers-other"):
        ex.submit(a, dict(sell, type="limit", limit_price="200", triggers=[{"side": "buy", "type": "limit", "limit_price": "90"}], **lot))
    assert ex.reserved_lots(a) == {} and not [o for o in ex.engine.orders.values() if o.active]


# ---- discovery (PR 20) x crypto (PR 14) x thread safety (PR 29) -----------------------------------------

def test_watchlist_rows_show_a_crypto_position_and_the_screener_filters_crypto(tmp_path):
    c, app, ex = make_app(tmp_path)
    aid = list(ex.accounts)[0]
    coin = next(s for s in ex.feed.symbols() if "/" in s)
    assert c.post(f"/api/accounts/{aid}/orders", json={"symbol": coin, "side": "buy", "qty": "0.01"}).status_code == 200
    assert c.post("/api/watchlist-sets", json={"name": "Coins", "symbols": [coin, "MKTX"]}).status_code == 200
    rows = c.get("/api/watchlist-sets/Coins/rows", params={"account": aid}).json()["rows"]
    by = {r["symbol"]: r for r in rows}
    assert D(str(by[coin]["position_qty"])) == D("0.01") and by["MKTX"]["position_qty"] is None
    r = c.post("/api/screener/run", json={"filters": [{"field": "type", "in": ["crypto"]}], "limit": 50}).json()
    assert r["rows"] and all("/" in x["symbol"] for x in r["rows"])
    assert any(x["symbol"] == coin for x in c.get("/api/search", params={"q": coin.split("/")[0], "types": "crypto"}).json())


def test_watchlist_rows_answer_busy_instead_of_waiting_while_the_lock_is_stuck(monkeypatch, tmp_path):
    monkeypatch.setattr(api_locking, "ROUTE_WAIT", 0.05)
    c, app, ex = make_app(tmp_path)
    with Holder(app.state.ctx.lock):
        r = c.get("/api/watchlist-sets/Main/rows")
        assert r.status_code == 503 and r.json()["busy"] is True
        assert c.get("/api/markets/overview").status_code == 200  # market data only: no exchange lock needed


# ---- hardening (PR 30) x backup uploads (PR 19) ---------------------------------------------------------

def test_a_backup_larger_than_the_general_body_limit_can_still_be_uploaded(tmp_path):
    """Hardening caps request bodies at 1 MB; a real backup is often larger. The restore upload has its own
    streamed 64 MB limit, which the access middleware must leave to the route."""
    import gzip
    import shutil
    from app.api import auth
    from app.marketdata.replay.library import BUNDLED_DIR
    c, app, ex = make_app(tmp_path)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    src = sorted(f for f in os.listdir(BUNDLED_DIR) if f.endswith(".gz"))[0]
    shutil.copy(os.path.join(BUNDLED_DIR, src), sessions / src)
    # pad the session file past the general limit with incompressible bytes inside a valid gzip member
    with open(sessions / src, "ab") as f:
        f.write(gzip.compress(os.urandom(auth.MAX_REQUEST_BODY + 200_000), compresslevel=0))
    data = c.get("/api/data/export").content
    assert len(data) > auth.MAX_REQUEST_BODY
    r = c.post("/api/data/restore/preview", content=data, headers={"content-type": "application/zip"})
    assert r.status_code != 413, r.text
    assert r.status_code == 200, r.text
    # the general limit still holds everywhere else, and the upload limit by declared length
    big = b"x" * (auth.MAX_REQUEST_BODY + 1)
    assert c.post("/api/watchlist-sets", content=big, headers={"content-type": "application/json"}).status_code == 413
    huge = {"content-type": "application/zip", "content-length": str(auth.UPLOAD_LIMITS["/api/data/restore/preview"] + 1)}
    assert c.post("/api/data/restore/preview", content=b"", headers=huge).status_code == 413


# ---- cash-account fix (PR 28) x crypto (PR 14) x lots (PR 13) -------------------------------------------

def test_a_cash_account_crypto_purchase_is_priced_with_its_fee_and_resting_buys_reserve_it():
    import pytest
    from app.exchange import OrderRejected
    ex, feed, clock, c, coin = _crypto()  # cash account with 10,000; coin at 40,000.00 ask
    a = ex.accounts[c]
    fee_rate = ex.crypto_fee_rate(a)
    # a resting limit buy reserves its cost and its fee
    ex.submit(c, {"symbol": coin, "side": "buy", "qty": "0.2", "type": "limit", "limit_price": "39000.00", "tif": "gtc"})
    reserved = D("0.2") * D("39000.00") * (1 + fee_rate / 100)
    left = a.cash - reserved
    with pytest.raises(OrderRejected):  # a second purchase may spend only what is left
        ex.submit(c, {"symbol": "ABC", "side": "buy", "qty": int(left / D("100.00")) + 1, "type": "limit",
                      "limit_price": "100.00", "tif": "gtc"})
    ex.submit(c, {"symbol": "ABC", "side": "buy", "qty": int(left / D("100.00")) - 1, "type": "limit",
                  "limit_price": "100.00", "tif": "gtc"})
    from test_crypto import set_coin
    for px in ("39000.00", "30000.00"):
        clock.advance(60)
        set_coin(feed, px)
        feed.set_quote("ABC", D("90.00"), D("90.01"), D("90.00"))
        ex.tick()
        assert a.cash >= 0


def test_a_cash_account_lot_sale_of_crypto_never_goes_short_or_below_zero():
    ex, feed, clock, c, coin = _crypto()
    _buy_lots(ex, feed, clock, c, coin, ["40000.00", "41000.00"], qty="0.05")
    a = ex.accounts[c]
    [o] = ex.submit(c, {"symbol": coin, "side": "sell", "qty": "0.05", "lots": [{"lot_id": "L2", "qty": "0.05"}]})
    assert o.status == "filled" and [l.id for l in a.lots[coin]] == ["L1"]
    assert a.position(coin) == D("0.05") and a.cash >= 0


# ---- backup (PR 19) x hardening reset validation (PR 30) ------------------------------------------------

def test_an_invalid_reset_takes_no_backup_and_a_valid_one_takes_it_before_changing_anything(tmp_path):
    import pytest
    c, app, ex = make_app(tmp_path)
    aid = list(ex.accounts)[0]
    calls = []
    ex.before_destructive = lambda kind: calls.append((kind, json.dumps(ex.accounts[aid].to_plain(), sort_keys=True)))
    before = json.dumps(ex.accounts[aid].to_plain(), sort_keys=True)
    with pytest.raises(ValueError):
        ex.reset_account(aid, "1e999999")
    assert calls == [] and json.dumps(ex.accounts[aid].to_plain(), sort_keys=True) == before
    ex.reset_account(aid, "5000")
    assert [k for k, _ in calls] == ["pre-reset"] and calls[0][1] == before


# ---- deterministic engine: same-tick fill order must not depend on the hash seed -------------------------

_SCENARIO = r'''
import json, sys
sys.path[:0] = ["tests", "."]
from helpers import D, T0, make_exchange, move
from app.core import calendar as cal
from app.options import chain
from app.options.symbology import build
ex, feed, clock = make_exchange(ABC="100.00")
a = ex.create_account("m", "margin", D("1000000")).id
c = ex.create_account("c", "cash", D("50000")).id
expiry = chain.expirations(cal.ny_date(T0))[0]
opts = [build("ABC", expiry, k, D(s)) for k in "CP" for s in (90, 95, 100, 105, 110)]
for sym in opts:
    feed.set_quote(sym, D("2.00"), D("2.10"), D("2.05"))
ex.tick()
for i, sym in enumerate(opts):
    ex.submit(a, {"symbol": sym, "side": "buy", "qty": 1 + i % 3, "type": "limit", "limit_price": "1.50", "tif": "gtc"})
    ex.submit(c, {"symbol": sym, "side": "buy", "qty": 1, "type": "limit", "limit_price": "1.40", "tif": "gtc"})
for acct in (a, c):
    for q in (10, 20, 30):
        ex.submit(acct, {"symbol": "ABC", "side": "buy", "qty": q, "type": "limit", "limit_price": "99.00", "tif": "gtc"})
for sym in opts:  # every option becomes marketable in the same tick
    feed.set_quote(sym, D("1.00"), D("1.05"), D("1.02"))
move(ex, feed, clock, "ABC", "98.00")
lot = ex.accounts[a].lots["ABC"][1]  # lot ids are per account, across symbols
ex.submit(a, {"symbol": "ABC", "side": "sell", "qty": str(lot.qty), "lots": [{"lot_id": lot.id, "qty": str(lot.qty)}]})
clock.advance(10 * 86400)  # past the expiry: settlements in one tick
move(ex, feed, clock, "ABC", "104.00")
out = [(f.account_id, f.symbol, str(f.qty), str(f.price), f.kind) for acct in ex.accounts.values() for f in acct.fills]
print(json.dumps(out))
'''


def test_same_tick_fills_and_expiry_settlements_do_not_depend_on_the_hash_seed():
    import subprocess
    import sys
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runs = []
    for seed in ("0", "1", "4242"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        r = subprocess.run([sys.executable, "-c", _SCENARIO], cwd=here, env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-2000:]
        runs.append(json.loads(r.stdout.strip().splitlines()[-1]))
    assert len(runs[0]) > 20, "the scenario did not produce the fills it was written for"
    assert runs[0] == runs[1] == runs[2]


# ---- cash-account fix (PR 28): a one-cancels-other pair on two symbols ------------------------------------

def test_a_cash_account_oco_sale_does_not_pay_for_its_other_member():
    """Found by the property test on main: an OCO of a stop buy (triggered at once) and a limit sale of another
    contract passed the check because the sale's 800 counted against the buy's 608; cash ended at -163.30."""
    from decimal import Decimal as Dec
    from test_orders_review import acct, at_expiry, NEAR, FAR, make_exchange as mk, move as mv
    ex, feed, clock = mk()
    a = acct(ex, "cash", "1000")
    mv(ex, feed, clock, "ABC", Dec("94.23"))
    ex.submit(a, {"symbol": "ABC261211C00090000", "side": "buy", "qty": 1})
    at_expiry(ex, feed, clock, Dec("100.07"), NEAR)
    cash = ex.account(a).cash
    assert Dec("0") < cash < Dec("600")
    import pytest
    from app.exchange import OrderRejected
    with pytest.raises(OrderRejected):
        ex.submit(a, {"oco": [{"symbol": "ABC261211C00095000", "side": "buy", "qty": 1, "tif": "gtc", "type": "stop", "stop_price": "6"},
                              {"symbol": "ABC261211C00090000", "side": "sell", "qty": 1, "tif": "gtc", "type": "limit", "limit_price": "8"}]})
    assert ex.account(a).cash == cash
    at_expiry(ex, feed, clock, Dec("60.00"), FAR)
    assert ex.account(a).cash >= 0


# ---- backup (PR 19) x thread safety (PR 29): the safety backup before a reset is written off the lock ------

def test_reset_takes_its_backup_outside_the_lock_and_a_failed_backup_stops_it(tmp_path, monkeypatch):
    from app.core.locking import held
    c, app, ex = make_app(tmp_path)
    ctx = app.state.ctx
    aid = list(ex.accounts)[0]
    assert c.post(f"/api/accounts/{aid}/orders", json={"symbol": "MKTX", "side": "buy", "qty": 3}).status_code == 200
    svc = ctx.data
    seen = []
    real = svc.backups.create

    def watched(kind, doc, market, info):
        seen.append((kind, held(), len(doc["accounts"][0]["fills"])))
        return real(kind, doc, market, info)
    monkeypatch.setattr(svc.backups, "create", watched)
    assert c.post(f"/api/accounts/{aid}/reset", json={"starting_cash": "5000"}).status_code == 200
    assert seen == [("pre-reset", False, 1)]  # one backup, written without the lock, holding the fill
    assert ex.accounts[aid].fills == []
    # a bad value is refused before any backup; a failing backup stops the reset
    assert c.post(f"/api/accounts/{aid}/reset", json={"starting_cash": "-1"}).status_code in (400, 422)
    assert len(seen) == 1
    c.post(f"/api/accounts/{aid}/orders", json={"symbol": "MKTX", "side": "buy", "qty": 2})
    from app.storage.backups import BackupError

    def broken(*a):
        raise BackupError("the disk is full")
    monkeypatch.setattr(svc.backups, "create", broken)
    r = c.post(f"/api/accounts/{aid}/reset", json={})
    assert r.status_code in (400, 409, 422) and "backup" in r.json()["detail"]
    assert len(ex.accounts[aid].fills) == 1  # nothing was reset
