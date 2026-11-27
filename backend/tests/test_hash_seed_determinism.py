"""The result of a tick must not depend on Python's string hash seed (review of PR 28, finding 12).

Option orders are matched when their underlying moves. The symbols used to come from a set, whose
order changes with PYTHONHASHSEED, so fills were numbered (and, when orders competed for the same
buying power, decided) in a different order from one run to the next.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = r'''
import json, sys
sys.path[:0] = ["tests", "."]
from helpers import D, make_exchange, move
from test_multileg import FAR, NEAR, acct, opt

ex, feed, clock = make_exchange()
a = acct(ex, "margin", "100000")
symbols = [opt("C", s, e) for s in (85, 90, 95, 100, 105) for e in (NEAR, FAR)]
symbols = [s for s in symbols if ex.quote(s).ask > D("0.50")]
for sym in symbols:  # just below the ask, so it rests until the calls get cheaper
    px = ex.quote(sym).ask - D("0.02")
    ex.submit(a, {"symbol": sym, "side": "buy", "qty": 1, "type": "limit", "limit_price": str(px), "tif": "gtc"})
assert not [e for e in ex.drain_events() if e["type"] == "fill"]
move(ex, feed, clock, "ABC", "95")
fills = [(e["fill"].id, e["fill"].symbol) for e in ex.drain_events() if e["type"] == "fill"]
print(json.dumps([len(symbols), fills]))
'''


def run(seed: str) -> list:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    out = subprocess.run([sys.executable, "-c", SCRIPT], cwd=Path(__file__).resolve().parents[1], env=env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_fills_on_one_tick_do_not_depend_on_the_hash_seed():
    count, first = run("1")
    assert count >= 6 and len(first) == count  # every resting option order filled on the move
    for seed in ("2", "3", "12345"):
        assert run(seed) == [count, first]
