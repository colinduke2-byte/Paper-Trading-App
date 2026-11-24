"""Measure what one save costs with a large ledger: the old full rewrite against the segment-based save.

Usage (from backend/): .venv/bin/python ../tools/measure_save.py [fills ...]
Prints, per size: state size, time and bytes of a full rewrite of everything (what every save did before),
and time and bytes of a save after one more trade (what a save does now).
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
from decimal import Decimal  # noqa: E402

from helpers import make_exchange, move  # noqa: E402
from app.storage import fsutil  # noqa: E402


def run(n: int) -> dict:
    d = tempfile.mkdtemp()
    ex, feed, clock = make_exchange(d)
    a = ex.create_account("Big", "margin", Decimal("100000000")).id
    for i in range(n):
        ex.submit(a, {"symbol": "ABC", "side": "buy" if i % 2 == 0 else "sell", "qty": 1})
        move(ex, feed, clock, "ABC", "100.00")
    ex.save(force=True)
    # before: serialise and rewrite the whole document
    t = time.perf_counter()
    full = json.dumps(ex.to_document()).encode()
    t_ser = time.perf_counter() - t
    t = time.perf_counter()
    fsutil.atomic_write_bytes(os.path.join(d, "full.json"), full)
    t_write = time.perf_counter() - t
    # after: one more trade, then a normal save
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1})
    move(ex, feed, clock, "ABC", "100.00")
    t = time.perf_counter()
    plan = ex.save_plan()
    t_plan = time.perf_counter() - t
    t = time.perf_counter()
    ex.write_plan(plan)
    t_w2 = time.perf_counter() - t
    b = ex.store.last_bytes
    return {"fills": n, "full_bytes": len(full), "full_serialise_ms": t_ser * 1000, "full_write_ms": t_write * 1000,
            "inc_state_bytes": b["state"], "inc_appended_bytes": b["appended"], "inc_plan_ms": t_plan * 1000,
            "inc_write_ms": t_w2 * 1000}


if __name__ == "__main__":
    sizes = [int(x) for x in sys.argv[1:]] or [1000, 5000]
    print("fills | full rewrite: bytes, serialise ms, write ms | after one trade: state.json bytes, appended bytes, "
          "serialise ms, write ms")
    for n in sizes:
        r = run(n)
        print(f"{r['fills']:>6} | {r['full_bytes']:>11,} {r['full_serialise_ms']:8.1f} {r['full_write_ms']:8.1f} | "
              f"{r['inc_state_bytes']:>9,} {r['inc_appended_bytes']:>7,} {r['inc_plan_ms']:8.1f} {r['inc_write_ms']:8.1f}")
