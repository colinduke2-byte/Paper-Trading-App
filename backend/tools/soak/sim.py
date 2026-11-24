"""Deterministic long-run simulator ("soak") driving the real Exchange.

    sim = Sim(seed=5, mode="always")
    result = sim.run(days=90)          # raises SoakFailure on the first broken invariant

A run is: a seeded SoakFeed (synthetic market plus extra corporate actions), a simulated clock
and three to five accounts with different types, lot methods and settings. A seeded Trader
proposes one command per step (orders of every kind, replaces, cancels, settings changes,
resets, price shocks, save/load cycles, offline gaps, replay sessions...); the simulator applies
it through the Exchange's public methods, ticks the market, and checks invariants:

- light checks after every step (ledger postings of new transactions, lots against position
  accounts, cash-account rules, expired options, every order that changed this step);
- full checks every `full_every` steps and at the end (ledger replay, cash identity, fills
  against orders, one-cancels-other groups, summary equity, every report and export route
  through the HTTP API, cross-checks between them, the state file and a lossless save/load).

Expected refusals (OrderRejected, ValueError, KeyError: what the API turns into 4xx answers)
are counted, any other exception is a failure. Everything is single-threaded and driven only by
the seed, so the same seed and length always give the same commands and the same state.
"""
import hashlib
import json
import os
import random
import shutil
import tempfile
import time
from collections import Counter
from decimal import Decimal
from typing import Optional

from app.core import calendar as cal
from app.core.clock import SimClock
from app.core.models import to_plain
from app.core.money import ZERO, D
from app.exchange import Exchange, OrderRejected
from app.lifecycle import settings as cfg
from app.marketdata.replay.format import SUFFIX
from app.marketdata.replay.library import BUNDLED_DIR
from app.marketdata.replay.samples import SAMPLES

from . import invariants as inv
from .feed import SoakFeed, shock
from .trader import Trader

START = 1767268800.0  # 2026-01-01 12:00 UTC
EXPECTED = (OrderRejected, ValueError, KeyError)
PROFILES = [
    ("margin", "fifo", {}),
    ("margin", "lifo", {"pattern_day_trader": False, "margin_interest_rate": "12"}),
    ("margin", "highest_cost", {"dividend_reinvestment": True, "margin_call_grace_hours": "0"}),
    ("margin", "fifo", {"margin_call_grace_hours": "1", "borrow_fee_rate": "25"}),
    ("cash", "fifo", {}),
    ("cash", "lifo", {"settlement": False}),
    ("cash", "highest_cost", {"settled_cash_only": False, "dividend_reinvestment": True}),
]
CASH_LEVELS = ("2000", "10000", "25000", "100000", "1000000")


class SoakFailure(AssertionError):
    def __init__(self, seed, mode, step, name, message, trace, cmd=None):
        self.seed, self.mode, self.step, self.name, self.message = seed, mode, step, name, message
        self.trace, self.cmd = trace, cmd
        super().__init__(f"seed {seed} ({mode}) step {step}: {name}: {message}\n  command: {json.dumps(cmd)}")


def state_text(ex) -> str:
    """What save() writes to state.json, as text (without touching the disk)."""
    return json.dumps({"version": 1, "accounts": [a.to_plain() for a in ex.accounts.values()],
                       "orders": [to_plain(o) for o in ex.engine.orders.values()],
                       "watchlists": ex.watchlists, "alerts": ex.alerts,
                       "seq": ex._peek_sequences(), "lifecycle": ex.lifecycle.to_plain()})


def snapshot(ex) -> str:
    return json.dumps({"accounts": [a.to_plain() for a in ex.accounts.values()],
                       "orders": [to_plain(o) for o in ex.engine.orders.values()],
                       "watchlists": ex.watchlists, "alerts": ex.alerts,
                       "lifecycle": ex.lifecycle.to_plain()}, sort_keys=True)


class Sim:
    def __init__(self, seed: int, mode: Optional[str] = None, data_dir: Optional[str] = None,
                 history_days: int = 40, full_every: int = 400, api_every: int = 2, api: bool = True,
                 replay_ops: bool = True, measure_market: bool = False):
        self.seed = seed
        rng0 = random.Random(f"soak-setup-{seed}")
        self.mode = mode or rng0.choice(("always", "always", "us"))
        self.t0 = START + rng0.randrange(0, 540) * 86400 + rng0.choice((0, 2.5, 3.6, 8.9, 9.4)) * 3600
        self.clock = SimClock(self.t0)
        self._own_dir = data_dir is None
        self.data_dir = data_dir or tempfile.mkdtemp(prefix=f"soak-{seed}-")
        os.makedirs(self.data_dir, exist_ok=True)
        self.history_days = history_days
        self.full_every, self.api_every, self.api, self.replay_ops = full_every, api_every, api, replay_ops
        self.measure_market = measure_market
        self.ex = self._new_exchange()
        self.trader = Trader(seed)
        self.stats = Counter()
        self.rejections = Counter()
        self.events = Counter()
        self.sizes = []  # (step, sim days, state bytes, activity units, market bytes, orders, ledger txns)
        self.trace: list = []
        self.step_no = 0
        self.book = inv.OrderBook()
        self._ledger_seen: dict = {}  # id(account) -> transactions already checked
        self._client = None
        self._full_count = 0
        self.wall = 0.0
        self.max_margin_call = 0.0
        for i in range(rng0.randint(3, 5)):
            kind, method, settings = PROFILES[rng0.randrange(len(PROFILES))]
            a = self.ex.create_account(f"{kind} {method} {i}", kind, Decimal(rng0.choice(CASH_LEVELS)))
            a.lot_method = method
            a.settings = cfg.update(a.settings, settings)
        self.ex.watchlists = {"Main": sorted(self.ex.feed.symbols())[:10]}
        self.ex.tick()
        self.ex.save(force=True)
        self._drain()

    # ---- plumbing ------------------------------------------------------------------------
    def _new_feed(self):
        return SoakFeed(self.seed, self.clock.now(), always_open=self.mode == "always",
                        history_days=self.history_days, start=self.t0)

    def _new_exchange(self):
        return Exchange(self._new_feed(), self.clock, data_dir=self.data_dir, session_mode=self.mode)

    def close(self) -> None:
        if self._own_dir:
            shutil.rmtree(self.data_dir, ignore_errors=True)

    def client(self):
        if self._client is None or self._client.app.state.ctx.live is not self.ex:
            from fastapi.testclient import TestClient
            from app.api.server import create_app
            self._client = TestClient(create_app(self.ex, run_loop=False), raise_server_exceptions=False)
        return self._client

    def _drain(self) -> None:
        for e in self.ex.drain_events():
            t = e.get("type")
            self.events[t if t != "lifecycle" else "lifecycle:" + e.get("kind", "")] += 1
            if t == "order":
                self.book.see(e["order"])
            elif t == "fill":
                self.events["fill:" + e["fill"].kind] += 1

    @property
    def days(self) -> float:
        return (self.clock.now() - self.t0) / 86400

    # ---- commands ------------------------------------------------------------------------
    def apply(self, cmd: dict) -> None:
        op = cmd["op"]
        self.stats[op] += 1
        try:
            getattr(self, "_op_" + op)(cmd)
        except EXPECTED as e:
            self.stats[op + ":refused"] += 1
            self.rejections[f"{op}: {type(e).__name__}: {_shape(str(e))}"] += 1
        finally:
            self._drain()

    def _tick(self, dt: float) -> None:
        self.clock.advance(dt)
        self.ex.tick()
        self._drain()
        inv.check_no_expired_options(self.ex, self.ex.now())

    def _op_tick(self, cmd):
        self._tick(cmd["dt"])

    def _op_run(self, cmd):
        left = cmd["dt"]
        while left > 0:
            step = min(cmd["every"], left)
            self._tick(step)
            left -= step

    def _op_submit(self, cmd):
        orders = self.ex.submit(cmd["acct"], cmd["req"])
        self.stats["accepted:" + (cmd.get("template") or _kind(cmd["req"]))] += 1
        for o in orders:
            self.book.see(o)

    def _op_preview(self, cmd):
        out = self.ex.preview(cmd["acct"], cmd["req"])
        if out["ok"] not in (True, False):
            inv.fail("preview_ok", f"preview ok is {out['ok']!r}")

    def _op_replace(self, cmd):
        self.book.see(self.ex.replace(cmd["acct"], cmd["order"], cmd["req"]))

    def _op_cancel(self, cmd):
        self.book.see(self.ex.cancel(cmd["acct"], cmd["order"]))

    def _op_cancel_all(self, cmd):
        self.ex.cancel_all(cmd["acct"])

    def _op_settings(self, cmd):
        a = self.ex.account(cmd["acct"])
        a.settings = cfg.update(a.settings, cmd["changes"])
        self.ex.dirty = True

    def _op_lot_method(self, cmd):
        self.ex.account(cmd["acct"]).lot_method = cmd["method"]
        self.ex.dirty = True

    def _op_shock(self, cmd):
        shock(self.ex.feed, cmd["symbol"], Decimal(cmd["factor"]))

    def _op_reset(self, cmd):
        gone = [o.id for o in self.ex.engine.orders.values() if o.account_id == cmd["acct"]]
        self.ex.reset_account(cmd["acct"], D(cmd["cash"]) if cmd.get("cash") else None)
        self.book.forget(gone)

    def _op_create_account(self, cmd):
        a = self.ex.create_account("extra", cmd["type"], D(cmd["cash"]))
        a.lot_method = cmd["lot_method"]

    def _op_delete_account(self, cmd):
        gone = [o.id for o in self.ex.engine.orders.values() if o.account_id == cmd["acct"]]
        self.ex.delete_account(cmd["acct"])
        self.book.forget(gone)

    def _op_journal(self, cmd):
        r = self.client().post(f"/api/accounts/{cmd['acct']}/journal",
                               json={k: cmd[k] for k in ("target_type", "target_id", "note", "tags") if k in cmd})
        if r.status_code >= 500:
            inv.fail("api_500", f"POST journal -> {r.status_code}: {r.text[:300]}")

    def _op_alert(self, cmd):
        self.ex.add_alert(cmd["symbol"], cmd["dir"], cmd["price"])

    def _op_reload(self, cmd):
        self._reload(cmd.get("force", True), check=True)

    def _op_offline(self, cmd):
        """The app is stopped (saved, or killed after its last periodic save), the clock moves on and
        the app starts again from its files."""
        self._reload(cmd.get("force", True), check=False, gap=cmd["days"] * 86400)
        self._tick(1)

    def _reload(self, force: bool, check: bool, gap: float = 0.0) -> None:
        old = self.ex
        old.save(force=force)
        path = os.path.join(self.data_dir, "state.json")
        before = open(path, "rb").read()
        if check and force:
            # the in-memory state and the file agree, and loading then saving changes nothing
            if state_text(old).encode() != before:
                inv.fail("save_not_current", "state.json after save(force=True) differs from the in-memory state")
            summaries = {aid: old.summary(aid) for aid in old.accounts}
            positions = {aid: old.positions(aid) for aid in old.accounts}
        self.clock.advance(gap)
        new = self._new_exchange()
        if not new.load():
            inv.fail("load_failed", "a state.json written by save() did not load")
        if check and force:
            if state_text(new).encode() != before:
                a, b = before.decode(), state_text(new)
                i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
                inv.fail("round_trip_not_lossless", f"load then save differs at byte {i}: {a[max(0, i - 80):i + 80]!r} vs "
                                                     f"{b[max(0, i - 80):i + 80]!r}")
            for aid in old.accounts:
                s_old, s_new = summaries[aid], new.summary(aid)
                diff = {k: (s_old[k], s_new.get(k)) for k in s_old if s_old[k] != s_new.get(k)}
                if diff:
                    inv.fail("reload_changes_summary", f"{aid}: {diff}")
                if positions[aid] != new.positions(aid):
                    inv.fail("reload_changes_positions", f"{aid} positions differ after load")
        self.ex = new
        self._ledger_seen = {}
        self._client = None
        self.stats["reloads"] += 1

    def _op_replay(self, cmd):
        if not self.replay_ops:
            return
        from app.marketdata.replay.control import ReplayController
        live = self.ex
        live.save(force=True)
        self._drain()
        before = snapshot(live)
        disk = open(os.path.join(self.data_dir, "state.json"), "rb").read()
        sid = SAMPLES[cmd["sample"]][0]
        c = ReplayController(os.path.join(BUNDLED_DIR, sid + SUFFIX), os.path.join(self.data_dir, "replay"))
        aid = next(iter(c.ex.accounts))
        try:
            for kind, arg in cmd["actions"]:
                try:
                    if kind == "submit":
                        c.ex.submit(aid, arg)
                    elif kind == "step":
                        if not c.ended:
                            c.step(arg)
                    elif kind == "seek":
                        c.seek(c.start + arg)
                except EXPECTED as e:
                    self.rejections[f"replay {kind}: {type(e).__name__}: {_shape(str(e))}"] += 1
            inv.check_ledger_full(c.ex.account(aid))
            inv.check_lots(c.ex.account(aid))
        finally:
            c.close()
        if snapshot(live) != before or open(os.path.join(self.data_dir, "state.json"), "rb").read() != disk:
            inv.fail("replay_touched_live", "live state changed while a replay ran")

    # ---- checks ---------------------------------------------------------------------------
    def check_light(self) -> None:
        for a in self.ex.accounts.values():
            key = id(a)
            self._ledger_seen[key] = inv.check_ledger_incremental(a, self._ledger_seen.get(key, 0))
            inv.check_lots(a)
            inv.check_cash_account(a)

    def split_symbols(self) -> set:
        return {r["symbol"] for r in self.ex.lifecycle.split_records.values()}

    def check_full(self) -> None:
        ex = self.ex
        self._full_count += 1
        splits = self.split_symbols()
        for a in ex.accounts.values():
            inv.check_ledger_full(a)
            inv.check_lots(a)
            inv.check_cash_account(a)
            inv.check_positions_vs_fills(a, splits)
            s = inv.check_summary(ex, a)
            if a.life.margin_call_since is not None:
                self.max_margin_call = max(self.max_margin_call, ex.now() - a.life.margin_call_since)
            if a.type == "cash" and s["settled_cash"] < 0:
                self.stats["soft:cash_account_negative_settled_cash"] += 1
        inv.check_orders_full(ex, self.book)
        text = state_text(ex)
        inv.scan_state_json(text)
        units = sum(len(a.ledger.transactions) + len(a.fills) + len(a.closed) + len(a.journal) + len(a.life.log)
                    + len(a.equity_history) for a in ex.accounts.values()) + len(ex.engine.orders) + len(ex.alerts)
        market = len(json.dumps(ex.feed.snapshot())) if self.measure_market else 0
        self.sizes.append((self.step_no, round(self.days, 2), len(text), units, market,
                           len(ex.engine.orders), sum(len(a.ledger.transactions) for a in ex.accounts.values())))
        if self.api and self._full_count % self.api_every == 0:
            self.check_api()

    def check_api(self) -> None:
        from .api_checks import check_routes
        check_routes(self, self.client())

    # ---- running ---------------------------------------------------------------------------
    def step(self, cmd: Optional[dict] = None) -> dict:
        cmd = cmd if cmd is not None else self.trader.next(self)
        self.step_no += 1
        self.trace.append(cmd)
        try:
            self.apply(cmd)
            self.check_light()
            if self.step_no % self.full_every == 0:
                self.check_full()
        except inv.InvariantError as e:
            raise SoakFailure(self.seed, self.mode, self.step_no, e.name, e.message, list(self.trace), cmd) from e
        except Exception as e:  # an unexpected exception is a failure too
            raise SoakFailure(self.seed, self.mode, self.step_no, f"exception:{type(e).__name__}",
                              f"{e!r} at {_where(e)}", list(self.trace), cmd) from e
        return cmd

    def run(self, days: float = 30, max_steps: Optional[int] = None, wall_limit: Optional[float] = None) -> dict:
        t = time.perf_counter()
        end = self.t0 + days * 86400
        while self.clock.now() < end and (max_steps is None or self.step_no < max_steps):
            self.step()
            if wall_limit is not None and time.perf_counter() - t > wall_limit:
                break
        try:
            self.check_full()
            if self.api:
                self.check_api()
        except inv.InvariantError as e:
            raise SoakFailure(self.seed, self.mode, self.step_no, e.name, e.message, list(self.trace), None) from e
        except Exception as e:
            raise SoakFailure(self.seed, self.mode, self.step_no, f"exception:{type(e).__name__}",
                              f"{e!r} at {_where(e)}", list(self.trace), None) from e
        self.wall = time.perf_counter() - t
        return self.result()

    def result(self) -> dict:
        self.ex.save(force=True)
        state = open(os.path.join(self.data_dir, "state.json"), "rb").read()
        return {"seed": self.seed, "mode": self.mode, "steps": self.step_no, "sim_days": round(self.days, 2),
                "wall_seconds": round(self.wall, 2), "state_bytes": len(state),
                "state_sha256": hashlib.sha256(state).hexdigest(), "stats": dict(self.stats),
                "events": dict(self.events), "sizes": self.sizes, "max_margin_call_hours": round(self.max_margin_call / 3600, 1)}


def replay(seed: int, mode: str, cmds: list, full_every: int = 50, api: bool = True) -> Optional[SoakFailure]:
    """Run exactly these commands from a fresh start. Returns the failure, or None."""
    sim = Sim(seed, mode=mode, full_every=full_every, api=api)
    try:
        for c in cmds:
            sim.step(c)
        sim.run(days=0)
    except SoakFailure as f:
        return f
    finally:
        sim.close()
    return None


def minimize(failure: SoakFailure, budget_seconds: float = 300, api: bool = True, log=None) -> list:
    """Delta-debug the command list down to a small one that still fails with the same name."""
    seed, mode, name = failure.seed, failure.mode, failure.name
    cmds = failure.trace[:failure.step]
    t0 = time.perf_counter()

    def fails(c):
        f = replay(seed, mode, c, api=api)
        return f is not None and f.name == name

    if not fails(cmds):  # a check every 50 steps may not see it in the same place; keep the full list
        return cmds
    n = 2
    while len(cmds) >= 2 and time.perf_counter() - t0 < budget_seconds:
        chunk = max(1, len(cmds) // n)
        reduced = False
        for i in range(0, len(cmds), chunk):
            trial = cmds[:i] + cmds[i + chunk:]
            if trial and fails(trial):
                cmds = trial
                n = max(n - 1, 2)
                reduced = True
                if log:
                    log(f"  minimised to {len(cmds)} commands")
                break
            if time.perf_counter() - t0 > budget_seconds:
                break
        if not reduced:
            if chunk == 1:
                break
            n = min(n * 2, len(cmds))
    return cmds


def _kind(req: dict) -> str:
    if req.get("legs") is not None:
        return "multileg"
    if req.get("oco") is not None:
        return "oco"
    if req.get("triggers") is not None:
        return "oto"
    if req.get("take_profit") or req.get("stop_loss"):
        return "bracket"
    return str(req.get("type", "market"))


def _shape(msg: str) -> str:
    """Rejection text with numbers and ids blanked, so similar refusals count together."""
    import re
    return re.sub(r"[A-Z]{2,6}\d{6}[CP]\d{8}|O\d{8}|A\d{4}|\d[\d,.]*", "#", msg)[:120]


def _where(e: BaseException) -> str:
    import traceback
    tb = traceback.extract_tb(e.__traceback__)
    app_frames = [f for f in tb if "/app/" in f.filename]
    f = (app_frames or tb)[-1]
    return f"{os.path.relpath(f.filename)}:{f.lineno} ({f.name})"
