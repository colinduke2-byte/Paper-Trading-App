"""The soak simulator (tools/soak) in the normal suite, and its long version.

Fast version (always runs, about 20 s): two seeded runs, one per session mode, full invariant and
HTTP route checks every 60 steps, a save/load cycle, an offline gap and a replay visit forced in.

Long version: PAPERDESK_SOAK=1, with PAPERDESK_SOAK_SEEDS (default 0-9) and PAPERDESK_SOAK_DAYS
(default 180). For campaigns over hundreds of seeds use the command line instead:
    python -m tools.soak --seeds 0-299 --days 365 --jobs 4
Confirmed failures (tests/soak/test_findings.py) are counted, not stopped on: see invariants.CONFIRMED.
"""
import os
import warnings

import pytest

from tools.soak import invariants as inv
from tools.soak.sim import Sim, SoakFailure

LONG = os.environ.get("PAPERDESK_SOAK") == "1"


@pytest.fixture(autouse=True)
def _confirmed_are_soft():
    warnings.simplefilter("ignore")
    saved = set(inv.KNOWN)
    inv.KNOWN.update(inv.CONFIRMED)
    inv.SOFT_SEEN.clear()
    yield
    inv.KNOWN.clear()
    inv.KNOWN.update(saved)


def run(seed, mode, **kw):
    sim = Sim(seed, mode=mode, full_every=kw.pop("full_every", 60), api_every=kw.pop("api_every", 2))
    try:
        for cmd in kw.pop("prelude", []):
            sim.step(cmd)
        return sim, sim.run(**kw)
    except SoakFailure as f:
        pytest.fail(str(f), pytrace=False)
    finally:
        sim.close()


@pytest.mark.parametrize("seed,mode", [(1, "always"), (2, "us")])
def test_fast_soak(seed, mode):
    prelude = [{"op": "submit", "acct": "A0001", "req": {"symbol": "NVLX", "side": "buy", "qty": 3}},
               {"op": "tick", "dt": 30}, {"op": "reload", "force": True},
               {"op": "offline", "days": 2, "force": False}, {"op": "replay", "sample": seed % 3,
                                                               "actions": [["submit", {"symbol": "NVLX", "side": "buy", "qty": 2}],
                                                                           ["step", 600]]}]
    sim, r = run(seed, mode, days=400, max_steps=170, prelude=prelude)
    assert r["steps"] >= 170 and r["stats"].get("reloads", 0) >= 2
    assert r["stats"].get("submit", 0) > 30


def test_same_seed_gives_byte_identical_state_twice_in_one_process():
    a = run(3, "always", days=400, max_steps=120, full_every=1000)[1]
    b = run(3, "always", days=400, max_steps=120, full_every=1000)[1]
    assert a["state_sha256"] == b["state_sha256"] and a["steps"] == b["steps"]


@pytest.mark.skipif(not LONG, reason="long soak: set PAPERDESK_SOAK=1")
def test_long_soak():
    spec = os.environ.get("PAPERDESK_SOAK_SEEDS") or "0-9"
    lo, _, hi = spec.partition("-")
    days = float(os.environ.get("PAPERDESK_SOAK_DAYS", "180"))
    for s in range(int(lo), int(hi or lo) + 1):
        run(s, None, days=days, full_every=400, api_every=4)
