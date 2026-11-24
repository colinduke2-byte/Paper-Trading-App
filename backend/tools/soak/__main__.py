"""Command line for the soak simulator. Run from backend/:

    python -m tools.soak --seed 7 --days 365                 one seed, print the result or the failure
    python -m tools.soak --seeds 0-299 --days 365 --jobs 4   many seeds in parallel, with totals
    python -m tools.soak --seed 7 --days 365 --minimize      on failure, delta-debug a minimal trace
    python -m tools.soak --seed 7 --trace failing.json       replay a saved command list

A failing run prints the seed, the step, the broken invariant and (with --minimize) the smallest
command list found that still breaks the same invariant, as JSON you can paste into a test.
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed

from .runner import _clean_env, run_one


def _seeds(text: str) -> list:
    out = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += range(int(a), int(b) + 1)
        elif part:
            out.append(int(part))
    return out


def determinism(seed: int, days: float, mode) -> int:
    import subprocess
    import tempfile
    out = tempfile.mkdtemp(prefix="soak-det-")
    texts = {}
    for label, hash_seed in (("process A (PYTHONHASHSEED=1)", "1"), ("process B (PYTHONHASHSEED=2)", "2"),
                             ("process C (PYTHONHASHSEED=1 again)", "1")):
        path = os.path.join(out, f"{len(texts)}.json")
        cmd = [sys.executable, "-m", "tools.soak", "--seed", str(seed), "--days", str(days), "--state-out", path]
        if mode:
            cmd += ["--mode", mode]
        subprocess.run(cmd, check=True, env=dict(os.environ, PYTHONHASHSEED=hash_seed))
        texts[label] = open(path, "rb").read()
    labels = list(texts)
    base = texts[labels[0]]
    same = True
    for label in labels[1:]:
        t = texts[label]
        if t == base:
            print(f"{label}: identical to {labels[0]} ({len(t)} bytes)")
            continue
        same = False
        i = next((k for k in range(min(len(t), len(base))) if t[k] != base[k]), min(len(t), len(base)))
        print(f"{label}: DIFFERS from {labels[0]} at byte {i}:\n  {base[max(0, i - 150):i + 150]!r}\n  "
              f"{t[max(0, i - 150):i + 150]!r}")
    return 0 if same else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.soak", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seed", type=int)
    p.add_argument("--seeds", help="e.g. 0-99 or 1,5,9")
    p.add_argument("--days", type=float, default=90, help="simulated days per seed")
    p.add_argument("--mode", choices=("always", "us"), help="session mode (default: chosen by the seed)")
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--full-every", type=int, default=400, help="steps between full checks")
    p.add_argument("--api-every", type=int, default=4, help="full checks between HTTP route checks")
    p.add_argument("--no-api", action="store_true", help="skip the HTTP route checks (faster)")
    p.add_argument("--minimize", action="store_true")
    p.add_argument("--budget", type=float, default=300, help="seconds to spend minimising one failure")
    p.add_argument("--trace", help="replay a JSON command list (from --trace-dir) instead of generating one")
    p.add_argument("--trace-dir", help="write each failing trace here")
    p.add_argument("--json", action="store_true", help="print one JSON line per seed")
    p.add_argument("--determinism", action="store_true",
                   help="run the seed twice in this process and once in each of two processes with different "
                        "PYTHONHASHSEED values, and compare the saved state byte for byte")
    p.add_argument("--state-out", help=argparse.SUPPRESS)
    p.add_argument("--known", default="confirmed", help="comma-separated invariant names to count instead of stopping on; "
                   "'confirmed' (default) means invariants.CONFIRMED, '' stops on everything")
    a = p.parse_args(argv)
    _clean_env()
    from .invariants import CONFIRMED

    if a.trace:
        from .sim import replay
        data = json.load(open(a.trace))
        f = replay(data["seed"], data["mode"], data["trace"], api=not a.no_api)
        print("passes" if f is None else str(f))
        return 0 if f is None else 1

    if a.state_out:  # child of --determinism: write the final state.json there
        import warnings
        warnings.simplefilter("ignore")
        from . import invariants as inv
        from .sim import Sim
        inv.KNOWN.update(CONFIRMED)
        sim = Sim(a.seed or 0, mode=a.mode, full_every=10 ** 9, api=False)
        sim.run(days=a.days)
        sim.ex.save(force=True)
        with open(os.path.join(sim.data_dir, "state.json"), "rb") as src, open(a.state_out, "wb") as dst:
            dst.write(src.read())
        sim.close()
        return 0
    if a.determinism:
        return determinism(a.seed or 0, a.days, a.mode)

    seeds = _seeds(a.seeds) if a.seeds else [a.seed if a.seed is not None else 0]
    t0 = time.perf_counter()
    results, failures = [], Counter()
    args = (a.days, a.mode, a.full_every, a.api_every, not a.no_api, a.minimize, a.budget, a.trace_dir,
            tuple(y for x in a.known.split(",") if x for y in (sorted(CONFIRMED) if x == "confirmed" else [x])))
    import multiprocessing
    # a fresh process per seed: memory freed by one run is not always returned to the system
    with ProcessPoolExecutor(max_workers=max(1, a.jobs), mp_context=multiprocessing.get_context("spawn"),
                             max_tasks_per_child=1) as pool:
        futs = {pool.submit(run_one, s, *args): s for s in seeds}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            if a.json:
                print(json.dumps(r, default=str), flush=True)
            elif r["ok"]:
                print(f"seed {r['seed']:>5} {r['mode']:>6} ok    {r['steps']:>6} steps {r['sim_days']:>7} days "
                      f"{r['wall_seconds']:>7}s  state {r['state_bytes'] / 1e6:.2f} MB  peak RSS {r.get('peak_rss_mb')} MB", flush=True)
            else:
                failures[r["name"]] += 1
                print(f"seed {r['seed']:>5} {r['mode']:>6} FAIL  step {r['step']}: {r['name']}: {r['message'][:300]}",
                      flush=True)
                if r.get("minimal_trace") is not None:
                    print("  minimal trace:", json.dumps(r["minimal_trace"]), flush=True)
    ok = [r for r in results if r["ok"]]
    steps = sum(r["steps"] for r in ok)
    days = sum(r["sim_days"] for r in ok) + sum(r.get("sim_days", 0) for r in results if not r["ok"])
    print(f"\n{len(results)} seeds, {len(ok)} passed, {len(results) - len(ok)} failed; {steps} steps, "
          f"{days / 365:.1f} simulated years, {time.perf_counter() - t0:.0f}s wall", flush=True)
    for name, n in failures.most_common():
        print(f"  {n:>4} x {name}")
    soft = Counter()
    for r in results:
        for name, (n, _) in (r.get("soft") or {}).items():
            soft[name] += 1
    for name, n in soft.most_common():
        print(f"  known (counted, not stopped) in {n} seeds: {name}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
