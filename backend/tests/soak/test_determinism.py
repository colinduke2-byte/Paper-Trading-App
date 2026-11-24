"""Determinism audit: the same seed and commands must give byte-identical state, in one process and
across processes (Python randomises str hashes per process, so set iteration order differs).

In-process repeatability is tested in test_soak.py. Across processes, see the reproduction below and
`python -m tools.soak --determinism --seed N --days D`, which compares whole soak runs.
"""
import os
import subprocess
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCRIPT = r"""
import sys
sys.path.insert(0, "tests")
from helpers import D, make_exchange, move
from app.core import calendar as cal
from app.options import chain
from app.options.symbology import build
ex, feed, clock = make_exchange()
a = ex.create_account("t", "margin", D("100000"))
exp = chain.expirations(cal.ny_date(ex.now()))[2]
for k in ("95", "100", "105", "110"):
    s = build("ABC", exp, "C", D(k))
    q = ex.quote(s)
    ex.submit(a.id, {"symbol": s, "side": "buy", "qty": 1, "type": "limit", "tif": "gtc",
                     "limit_price": str((q.bid * D("0.8")).quantize(D("0.01")))})
move(ex, feed, clock, "ABC", "90.00")  # every call is now cheap enough: all four fill on this tick
print(",".join(f"{f.id}={f.symbol[-8:]}" for f in a.fills))
"""


def run_with_hash_seed(seed: int) -> str:
    env = dict(os.environ, PYTHONHASHSEED=str(seed), PYTHONPATH=BACKEND)
    return subprocess.run([sys.executable, "-c", SCRIPT], cwd=BACKEND, env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


def test_the_reproduction_fills_every_order_on_one_tick():
    out = run_with_hash_seed(0)
    assert out.count("=") == 4


@pytest.mark.xfail(strict=True, reason=(
    "exchange.py tick(): `active_symbols` is a set of symbols and the loop `for sym in active_symbols` "
    "(re-pricing working option orders when their underlying moved) runs in str-hash order, which changes "
    "with PYTHONHASHSEED; option orders that fill on the same tick get fill ids, ledger transactions and "
    "fills in a different order in every process, so state.json is not reproducible across runs"))
def test_option_fills_on_one_tick_come_in_the_same_order_in_every_process():
    outs = {run_with_hash_seed(s) for s in range(6)}
    assert len(outs) == 1, outs
