"""One soak run in a worker process (importable, so spawned workers can find it)."""
import json
import os
import time


def _clean_env() -> None:
    for k in ("PAPERDESK_LAN", "PAPERDESK_PASSCODE", "PAPERDESK_SESSION_DAYS", "PAPERDESK_LOOPBACK_BYPASS",
              "PAPERDESK_ALLOWED_HOSTS", "PAPERDESK_BIND", "PAPERDESK_ENV_FILE"):
        os.environ.pop(k, None)


def run_one(seed: int, days: float, mode, full_every: int, api_every: int, api: bool, minimize: bool,
            budget: float, trace_dir, known=()) -> dict:
    _clean_env()
    import warnings
    warnings.simplefilter("ignore")
    from . import invariants as inv
    from .sim import Sim, SoakFailure, minimize as mini
    inv.KNOWN.update(known)
    sim = Sim(seed, mode=mode, full_every=full_every, api_every=api_every, api=api)
    t = time.perf_counter()
    try:
        r = sim.run(days=days)
        r["ok"] = True
        r["rejections"] = dict(sim.rejections)
        r["soft"] = dict(inv.SOFT_SEEN)
        import resource
        r["peak_rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
        r.pop("sizes", None)
        r["size_curve"] = sim.sizes[::max(1, len(sim.sizes) // 20)]
        return r
    except SoakFailure as f:
        out = {"ok": False, "seed": seed, "mode": f.mode, "step": f.step, "name": f.name, "message": f.message,
               "cmd": f.cmd, "soft": dict(inv.SOFT_SEEN), "wall_seconds": round(time.perf_counter() - t, 1), "sim_days": round(sim.days, 2)}
        if minimize:
            small = mini(f, budget_seconds=budget, api=api)
            out["minimal_trace"] = small
        if trace_dir:
            os.makedirs(trace_dir, exist_ok=True)
            with open(os.path.join(trace_dir, f"seed{seed}-{f.name.replace(':', '_')}.json"), "w") as fh:
                json.dump({"seed": seed, "mode": f.mode, "trace": out.get("minimal_trace") or f.trace[:f.step]}, fh)
        return out
    finally:
        sim.close()
