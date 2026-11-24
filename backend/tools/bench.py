"""Performance and scale benchmarks for the exchange and the API (not part of the test suite).

    cd backend && python -m tools.bench            # every scenario, a markdown table on stdout
    python -m tools.bench --only orders,ledger     # some scenarios
    python -m tools.bench --profile orders         # cProfile of the slowest case of a scenario

Scenarios (each measures one tick of the 1 s server loop, ex.tick() then ex.save(), plus the parts):
  symbols    26 / 500 / 5000 listed symbols (synthetic universe cloned)
  orders     10 / 100 / 1000 / 5000 working orders; and 5000 finished orders kept in the book
  options    1 / 10 / 50 / 200 held option positions in one margin account
  accounts   1 / 5 / 20 accounts, each with stock and option positions
  ledger     1e3 / 1e4 / 1e5 ledger transactions: save() time and size, load() time, API route latency
  websocket  payload of one tick message for a client watching 300 symbols and a large account
A tick is "unusable" when it takes longer than the 1 s loop period (the loop then falls behind and
every request waits on the lock the tick holds).
"""
import argparse
import cProfile
import io
import json
import os
import pstats
import shutil
import statistics
import string
import sys
import tempfile
import time
import warnings
from dataclasses import replace
from decimal import Decimal

warnings.simplefilter("ignore")

from app.core import calendar as cal  # noqa: E402
from app.core.clock import SimClock  # noqa: E402
from app.core.models import Fill  # noqa: E402
from app.exchange import Exchange  # noqa: E402
from app.marketdata.synthetic import SyntheticFeed  # noqa: E402
from app.options import chain as optchain  # noqa: E402
from app.options.symbology import build  # noqa: E402

T0 = 1795500000.0
D = Decimal
ROWS = []


def row(scenario, case, **vals):
    ROWS.append((scenario, case, vals))
    print(f"  {scenario:<10} {case:<34} " + "  ".join(f"{k}={v}" for k, v in vals.items()), flush=True)


def big_feed(n: int, now: float = T0) -> SyntheticFeed:
    feed = SyntheticFeed(seed=1, now=now, history_days=30)
    base = sorted(feed.info)
    letters = string.ascii_uppercase
    i = 0
    while len(feed.info) < n:
        src = base[i % len(base)]
        name = "Z" + "".join(letters[(i // 26 ** k) % 26] for k in range(4))
        feed.info[name] = dict(feed.info[src], symbol=name)
        feed.quotes[name] = replace(feed.quotes[src], symbol=name)
        feed._day[name] = feed._day[src]
        feed.store.daily[name] = list(feed.store.daily[src])
        i += 1
    return feed


def exchange(n_symbols=26, data_dir=None) -> Exchange:
    feed = big_feed(n_symbols) if n_symbols != 26 else SyntheticFeed(seed=1, now=T0, history_days=30)
    return Exchange(feed, SimClock(T0), data_dir=data_dir)


def time_ticks(ex, n=20, dt=1.0, save=True) -> dict:
    lat, cpu = [], []
    for _ in range(n):
        ex.clock.advance(dt)
        w, c = time.perf_counter(), time.process_time()
        ex.tick()
        if save:
            ex.save()
        lat.append(time.perf_counter() - w)
        cpu.append(time.process_time() - c)
        ex.drain_events()
    lat.sort()
    return {"tick_ms": round(statistics.mean(lat) * 1000, 1), "p95_ms": round(lat[int(len(lat) * 0.95) - 1] * 1000, 1),
            "cpu_ms": round(statistics.mean(cpu) * 1000, 1)}


def timed(fn, repeat=3):
    best = None
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn()
        d = time.perf_counter() - t
        best = d if best is None else min(best, d)
    return out, round(best * 1000, 1)


# ---- scenarios -----------------------------------------------------------------------------------
def bench_symbols(cases=(26, 500, 5000)):
    for n in cases:
        d = tempfile.mkdtemp()
        ex = exchange(n, d)
        a = ex.create_account("m", "margin", D(1_000_000))
        ex.tick()
        for s in sorted(ex.feed.info)[:10]:
            ex.submit(a.id, {"symbol": s, "side": "buy", "qty": 10})
        r = time_ticks(ex, 15)
        ex.save(force=True)
        row("symbols", f"{n} symbols", **r, market_json_mb=round(os.path.getsize(os.path.join(d, "market.json")) / 1e6, 2))
        shutil.rmtree(d)


def _orders_exchange(n_working, n_done=0):
    ex = exchange()
    a = ex.create_account("m", "margin", D(10_000_000_000) / 20)
    ex.tick()
    syms = sorted(ex.feed.info)
    t = time.perf_counter()
    from app.core.models import Order
    for i in range(n_done):  # finished orders as the book keeps them (filled or canceled), put in directly
        o = Order(id=f"H{i:08d}", account_id=a.id, symbol=syms[i % len(syms)], side="buy", qty=D(1), type="limit",
                  limit_price=D("1.00"), tif="gtc", status="canceled" if i % 2 else "filled", created_at=T0)
        ex.engine.orders[o.id] = o
    placed = []
    for i in range(n_working):
        s = syms[i % len(syms)]
        w = time.perf_counter()
        ex.submit(a.id, {"symbol": s, "side": "buy", "qty": 1, "type": "limit", "tif": "gtc",
                         "limit_price": str((ex.feed.quote(s).last * D("0.5")).quantize(D("0.01")))})
        placed.append(time.perf_counter() - w)
    return ex, a, placed, time.perf_counter() - t


def bench_orders(cases=(10, 100, 1000, 5000)):
    for n in cases:
        ex, a, placed, total = _orders_exchange(n)
        r = time_ticks(ex, 10, save=False)
        _, summ = timed(lambda: ex.summary(a.id))
        row("orders", f"{n} working orders", **r, last_submit_ms=round(placed[-1] * 1000, 1),
            place_all_s=round(total, 1), summary_ms=summ)
    for done in (5000, 50000):
        ex, a, placed, total = _orders_exchange(10, done)
        r = time_ticks(ex, 10, save=False)
        _, summ = timed(lambda: ex.summary(a.id))
        row("orders", f"10 working + {done} finished", **r, last_submit_ms=round(placed[-1] * 1000, 1), summary_ms=summ)


def _option_symbols(ex, n):
    out = []
    today = cal.ny_date(ex.now())
    exps = optchain.expirations(today)
    for u in sorted(ex.feed.info):
        spot = float(ex.feed.quote(u).last)
        for e in exps:
            for k in optchain.strikes(spot)[::3]:
                for kind in "CP":
                    out.append(build(u, e, kind, k))
                    if len(out) >= n:
                        return out
    return out


def _hold(ex, a, symbols, qty=1, price=D("1.00")):
    for i, s in enumerate(symbols):
        f = Fill(id=f"B{i}", order_id=None, account_id=a.id, symbol=s, side="buy" if i % 3 else "sell",
                 qty=D(qty), price=price, ts=ex.now())
        a.apply_fill(f, 100 if len(s) > 6 else 1)


def bench_options(cases=(1, 10, 50, 200)):
    for n in cases:
        ex = exchange()
        a = ex.create_account("m", "margin", D(10_000_000))
        ex.tick()
        _hold(ex, a, _option_symbols(ex, n))
        r = time_ticks(ex, 10, dt=61, save=False)  # dt 61: the equity sample (a summary per account) runs every tick
        _, summ = timed(lambda: ex.summary(a.id))
        _, pos = timed(lambda: ex.positions(a.id))
        row("options", f"{n} option positions", **r, summary_ms=summ, positions_ms=pos)


def bench_accounts(cases=(1, 5, 20)):
    for n in cases:
        ex = exchange()
        ex.tick()
        opts = _option_symbols(ex, 20)
        for i in range(n):
            a = ex.create_account(f"a{i}", "margin", D(1_000_000))
            _hold(ex, a, opts)
            _hold(ex, a, sorted(ex.feed.info)[:10], qty=10, price=D("100"))
        r = time_ticks(ex, 10, dt=61, save=False)
        row("accounts", f"{n} accounts x 30 positions", **r)


def _ledger_exchange(n_txn, d):
    ex = exchange(data_dir=d)
    a = ex.create_account("m", "margin", D(10_000_000))
    ex.tick()
    syms = sorted(ex.feed.info)
    for i in range(n_txn):
        s = syms[i % 7]
        f = Fill(id=f"F{i:08d}", order_id=None, account_id=a.id, symbol=s, side="buy" if (i // 7) % 2 == 0 else "sell",
                 qty=D(10), price=D(100) + D(i % 50) / 10, ts=ex.now() + i)
        a.apply_fill(f, 1)
    ex.clock.advance(n_txn + 10)
    a.equity_history = [[int(T0) + 60 * k, "10000000"] for k in range(6000)]
    return ex, a


def bench_ledger(cases=(1000, 10000, 30000, 100000)):
    from fastapi.testclient import TestClient
    from app.api.server import create_app
    for n in cases:
        d = tempfile.mkdtemp()
        ex, a = _ledger_exchange(n, d)
        _, save_ms = timed(lambda: ex.save(force=True), repeat=1)
        size = os.path.getsize(os.path.join(d, "state.json"))
        ex2 = exchange(data_dir=d)
        _, load_ms = timed(lambda: ex2.load(), repeat=1)
        c = TestClient(create_app(ex, run_loop=False))
        month = cal.ny_date(ex.now()).strftime("%Y-%m")
        lat = {}
        routes = (("summary", ""), ("positions", "/positions"), ("orders", "/orders"),
                          ("analytics", "/analytics"), ("gains", "/gains"), ("statement", f"/statements/{month}"),
                          ("ledger", "/ledger"), ("csv_ledger", "/export/ledger.csv"))
        if n > 30000:  # gains and analytics are quadratic in closed lots: minutes at this size
            routes = tuple(r for r in routes if r[0] not in ("gains", "analytics"))
        for name, url in routes:
            r, ms = timed(lambda: c.get(f"/api/accounts/{a.id}{url}"), repeat=1)
            assert r.status_code == 200, (url, r.status_code)
            lat[name + "_ms"] = ms
        tick = time_ticks(ex, 5, save=True)
        row("ledger", f"{n} transactions", state_mb=round(size / 1e6, 2), save_ms=save_ms, load_ms=load_ms,
            loop_tick_ms=tick["tick_ms"], **lat)
        shutil.rmtree(d)


def bench_save_encoder():
    """The same state written with json.dump (what save() does) and with json.dumps."""
    d = tempfile.mkdtemp()
    ex, a = _ledger_exchange(20000, d)
    data = {"accounts": [x.to_plain() for x in ex.accounts.values()]}
    market = ex.feed.snapshot()
    for label, obj in (("state, 20000 txns", data), ("market.json", market)):
        _, dump_ms = timed(lambda: json.dump(obj, io.StringIO()), repeat=2)
        _, dumps_ms = timed(lambda: io.StringIO().write(json.dumps(obj)), repeat=2)
        row("encoder", label, json_dump_ms=dump_ms, json_dumps_ms=dumps_ms, bytes=len(json.dumps(obj)))
    shutil.rmtree(d)


def bench_websocket():
    from app.api.server import plain, quote_row
    ex = exchange(500)
    a = ex.create_account("m", "margin", D(10_000_000))
    ex.tick()
    for n in (1, 50, 200):
        a2 = ex.create_account(f"o{n}", "margin", D(10_000_000))
        _hold(ex, a2, _option_symbols(ex, n))
        syms = sorted(ex.feed.info)[:300]
        msg = {"type": "tick", "status": {"time": ex.now()}, "quotes": {s: quote_row(ex, s) for s in syms},
               "account": plain(ex.summary(a2.id)), "positions": plain(ex.positions(a2.id)), "events": []}
        _, build_ms = timed(lambda: {"q": {s: quote_row(ex, s) for s in syms}, "p": plain(ex.positions(a2.id)),
                                     "s": plain(ex.summary(a2.id))}, repeat=2)
        row("websocket", f"300 symbols, {n} option positions", payload_kb=round(len(json.dumps(msg)) / 1024, 1),
            build_ms=build_ms)


SCENARIOS = {"symbols": bench_symbols, "orders": bench_orders, "options": bench_options, "accounts": bench_accounts,
             "ledger": bench_ledger, "encoder": bench_save_encoder, "websocket": bench_websocket}


def profile(name: str) -> None:
    pr = cProfile.Profile()
    if name == "orders":
        ex, a, _, _ = _orders_exchange(2000, 3000)
        pr.enable()
        time_ticks(ex, 5, save=False)
        ex.summary(a.id)
    elif name == "options":
        ex = exchange()
        a = ex.create_account("m", "margin", D(10_000_000))
        ex.tick()
        _hold(ex, a, _option_symbols(ex, 200))
        pr.enable()
        time_ticks(ex, 5, dt=61, save=False)
    elif name == "ledger":
        d = tempfile.mkdtemp()
        ex, a = _ledger_exchange(50000, d)
        pr.enable()
        ex.save(force=True)
    else:
        ex = exchange(5000)
        ex.create_account("m", "margin")
        ex.tick()
        pr.enable()
        time_ticks(ex, 5)
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(18)
    print(s.getvalue())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.bench", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", help="comma-separated scenarios: " + ", ".join(SCENARIOS))
    p.add_argument("--profile", choices=("orders", "options", "ledger", "symbols"))
    a = p.parse_args(argv)
    if a.profile:
        profile(a.profile)
        return 0
    for name in (a.only.split(",") if a.only else SCENARIOS):
        SCENARIOS[name]()
    print("\n| scenario | case | measurements |\n|---|---|---|")
    for sc, case, vals in ROWS:
        print(f"| {sc} | {case} | " + ", ".join(f"{k} {v}" for k, v in vals.items()) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
