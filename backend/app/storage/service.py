"""Data and backups for a running server: automatic backups, snapshots before destructive actions, restore,
account import and the recovery choices. Used by app/api/routes_data.py.

Locking: everything that reads or replaces the live exchange's state does so under ctx.lock (the lock the tick
loop holds while it ticks and saves). Serialising happens under the lock; writing the archive happens outside
it. Nothing here runs while a replay is active (the live exchange is parked then) or during a mode switch
(enter_replay / exit_replay hold the same lock).
"""
import secrets
import threading
import time
import weakref
from typing import Callable, Optional

from .. import __version__
from . import archive, schema
from .backups import BackupError, BackupManager
from .store import log

PENDING_SECONDS = 900.0
SCHEDULER_PERIOD = 30.0


class DataService:
    def __init__(self, ctx, wall: Callable[[], float] = time.time, start_scheduler: bool = True):
        self._ctx = weakref.ref(ctx)
        self.wall = wall
        live = ctx.live
        self.backups = BackupManager(live.data_dir, wall) if live.data_dir else None
        self.pending: dict = {}
        self._plock = threading.Lock()
        self._stop = threading.Event()
        self.thread = None
        if self.backups is not None:
            live.before_destructive = self.snapshot_before
            lock = getattr(live, "data_lock", None)
            if start_scheduler and (self.backups.interval > 0 or (lock is not None and lock.fallback)):
                self.thread = threading.Thread(target=self._run, args=(self._ctx, self._stop), daemon=True,
                                               name="paperdesk-backups")
                self.thread.start()

    @property
    def ctx(self):
        c = self._ctx()
        if c is None:
            raise RuntimeError("the server has stopped")
        return c

    # ---- information --------------------------------------------------------------------------
    def info(self, ex) -> dict:
        return {"app_version": __version__, "schema_version": schema.CURRENT_VERSION, "feed": ex.feed.name,
                "simulated": bool(ex.feed.simulated), "accounts": len(ex.accounts)}

    def status(self) -> dict:
        ctx = self.ctx
        live = ctx.live
        st = live.store
        rows = self.backups.list() if self.backups else []
        lock = getattr(live, "data_lock", None)
        return {"enabled": st is not None, "data_dir": live.data_dir, "mode": ctx.mode,
                "save": st.status() if st else None,
                "recovery": st.recovery if st else None,
                "schema_version": schema.CURRENT_VERSION, "app_version": __version__,
                "loaded_from_version": getattr(live.load_result, "from_version", None),
                "backups": {"count": len(rows), "latest": rows[0] if rows else None,
                            "interval_seconds": self.backups.interval if self.backups else 0,
                            "keep_hourly": self.backups.keep_hourly if self.backups else 0,
                            "keep_daily": self.backups.keep_daily if self.backups else 0,
                            "last_error": self.backups.last_error if self.backups else None},
                "locked": bool(lock and lock.held)}

    # ---- snapshots ----------------------------------------------------------------------------
    def _serialise(self, ex) -> tuple:
        return ex.to_document(), (ex.feed.snapshot() or None), self.info(ex)

    def snapshot(self, kind: str) -> dict:
        """Back up the live state now (serialise under the lock, write outside it)."""
        if self.backups is None:
            raise BackupError("this server has no data folder, so there is nothing to back up")
        ctx = self.ctx
        with ctx.lock:
            if ctx.replay is not None:
                raise BackupError("backups are not taken while a replay runs; exit the replay first")
            st = ctx.live.store
            if st is not None and st.blocked:
                raise BackupError("the data folder is in recovery; choose what to do with it first")
            doc, market, info = self._serialise(ctx.live)
        return self.backups.create(kind, doc, market, info)

    def snapshot_before(self, kind: str) -> None:
        """Called by the exchange before a reset or delete (already under the lock). A failed snapshot stops
        the action: better to refuse than to destroy data without a way back."""
        ctx = self._ctx()
        if ctx is None or self.backups is None:
            return
        ex = ctx.live
        st = ex.store
        if ctx.replay is not None or (st is not None and st.blocked):
            return  # the sandbox has no backups; in recovery there is nothing worth keeping in memory
        try:
            doc, market, info = self._serialise(ex)
            self.backups.create(kind, doc, market, info)
        except BackupError as e:
            raise ValueError(f"the action was stopped because the backup before it failed: {e}") from None

    def _run(self, ref, stop) -> None:
        while not stop.wait(SCHEDULER_PERIOD):
            ctx = ref()
            if ctx is None:
                return
            svc = getattr(ctx, "data", None)
            del ctx
            if svc is None:
                continue
            try:
                svc.tick()
            except Exception as e:  # keep the scheduler alive; the error shows in the status
                log.error("automatic backup failed: %r", e)

    def tick(self) -> Optional[dict]:
        """Take an automatic backup when one is due. Returns its row, or None."""
        ctx = self.ctx
        lock = getattr(ctx.live, "data_lock", None)
        if lock is not None and lock.fallback:
            lock.heartbeat()  # the lock file is the only guard on file systems without locking
        if self.backups is None or not self.backups.due():
            return None
        with ctx.lock:
            st = ctx.live.store
            if ctx.replay is not None or (st is not None and st.blocked):
                return None
            doc, market, info = self._serialise(ctx.live)
        try:
            row = self.backups.create("auto", doc, market, info)
        except BackupError as e:
            log.error("%s", e)
            return None
        if st is not None:
            st.compact_next()  # rewrite the segment files from memory at the next save
        return row

    def stop(self) -> None:
        self._stop.set()

    # ---- pending restores and imports -------------------------------------------------------------
    def _keep(self, kind: str, payload) -> str:
        token = secrets.token_urlsafe(16)
        now = self.wall()
        with self._plock:
            for k in [k for k, v in self.pending.items() if now - v["at"] > PENDING_SECONDS]:
                del self.pending[k]
            # one pending item of each kind: the newest preview wins
            for k in [k for k, v in self.pending.items() if v["kind"] == kind]:
                del self.pending[k]
            self.pending[token] = {"kind": kind, "payload": payload, "at": now}
        return token

    def _take(self, token: str, kind: str):
        with self._plock:
            item = self.pending.get(str(token))
            if item is None or item["kind"] != kind or self.wall() - item["at"] > PENDING_SECONDS:
                raise LookupError("that preview has expired or was replaced; preview it again")
            del self.pending[str(token)]
        return item["payload"]

    def preview_restore(self, arc: archive.Archive, source: str) -> dict:
        ctx = self.ctx
        with ctx.lock:
            summary = archive.summarize(ctx.live, arc, set(ctx.live.feed.symbols()))
        summary["source"] = source
        summary["token"] = self._keep("restore", arc)
        return summary

    def apply_restore(self, token: str) -> dict:
        arc = self._take(token, "restore")
        return self.restore(arc)

    def restore(self, arc: archive.Archive, snapshot_kind: str = "pre-restore") -> dict:
        """Replace the live state with an archive. A snapshot of the current state is taken first."""
        ctx = self.ctx
        with ctx.lock:
            if ctx.replay is not None:
                raise RuntimeError("exit the replay before restoring a backup")
            ex = ctx.live
            st = ex.store
            in_recovery = st is not None and st.blocked
            if in_recovery:
                st.ensure_copy()  # raises: then nothing is restored (the damaged files would be lost)
            if self.backups is not None and not in_recovery:
                doc, market, info = self._serialise(ex)
                self.backups.create(snapshot_kind, doc, market, info)  # raises: then nothing is restored
            # Files first (the step most likely to fail), then memory; a failure in memory puts the old state back.
            self._write_sessions(ex, arc.sessions)
            import json
            before = ex.to_document()
            before_market = ex.feed.snapshot() if arc.market is not None else None
            try:
                ex.apply_document(json.loads(json.dumps(arc.doc)))  # a copy: the exchange must not share lists with it
                if arc.market is not None:
                    ex.feed.restore(arc.market)
                    ex.lifecycle.repair_feed()
            except Exception:
                ex.apply_document(before)
                if before_market:
                    ex.feed.restore(before_market)
                raise
            if st is not None:
                if in_recovery:
                    st.end_recovery()
                st.compact_next()
            ex.dirty = True
            ex._last_market_save = float("-inf")
            ok = ex.save(force=True)
            st_status = st.status() if st else None
        return {"ok": True, "saved": ok, "accounts": len(ex.accounts), "save": st_status}

    @staticmethod
    def _write_sessions(ex, sessions: dict) -> None:
        if not sessions or not ex.data_dir:
            return
        import os
        from .fsutil import atomic_write_bytes
        d = os.path.join(ex.data_dir, "sessions")
        os.makedirs(d, exist_ok=True)
        for name, data in sessions.items():  # names were checked by archive.read (no path parts)
            atomic_write_bytes(os.path.join(d, name), data)

    def preview_import(self, raw: dict) -> dict:
        ctx = self.ctx
        with ctx.lock:
            if ctx.replay is not None:
                raise RuntimeError("exit the replay before importing an account")
            imp = archive.prepare_import(ctx.live, raw)
        a = imp.account
        return {"token": self._keep("import", raw), "name": a["name"], "type": a["type"], "old_id": imp.old_id,
                "new_id": imp.new_id, "cash": _cash(a), "positions": len(a["lots"]),
                "fills": len(a["fills"]), "orders": len(imp.orders),
                "active_orders": sum(1 for o in imp.orders if o["status"] in ("held", "working", "partially_filled")),
                "missing_symbols": imp.missing_symbols, "canceled_orders": len(imp.canceled), "source": imp.source}

    def apply_import(self, token: str, name: Optional[str] = None) -> dict:
        raw = self._take(token, "import")
        ctx = self.ctx
        with ctx.lock:
            if ctx.replay is not None:
                raise RuntimeError("exit the replay before importing an account")
            ex = ctx.live
            imp = archive.prepare_import(ex, raw)  # fresh ids: orders may have been placed since the preview
            if self.backups is not None:
                doc, market, info = self._serialise(ex)
                self.backups.create("pre-import", doc, market, info)
            aid = archive.apply_import(ex, imp, name)
            ex.save(force=True)
        return {"ok": True, "account_id": aid, "missing_symbols": imp.missing_symbols,
                "canceled_orders": len(imp.canceled)}

    # ---- recovery -----------------------------------------------------------------------------------
    def recovery_options(self) -> dict:
        ctx = self.ctx
        st = ctx.live.store
        rows = self.backups.list() if self.backups else []
        return {"recovery": st.recovery if st else None, "backups": rows[:30]}

    def recover_restore(self, name: Optional[str]) -> dict:
        st = self.ctx.live.store
        if st is None or not st.blocked:
            raise RuntimeError("the data folder is not in recovery")
        if self.backups is None:
            raise LookupError("there are no backups")
        if name:
            arc = self.backups.read(name)
        else:
            got = self.backups.latest_good()
            if got is None:
                raise LookupError("no backup that passes its checks was found")
            arc = got[1]
        return self.restore(arc)

    def recover_fresh(self, create_defaults: Callable) -> dict:
        ctx = self.ctx
        with ctx.lock:
            ex = ctx.live
            st = ex.store
            if st is None or not st.blocked:
                raise RuntimeError("the data folder is not in recovery")
            st.ensure_copy()  # raises: then the damaged files stay as they are
            ex.accounts, ex.engine.orders = {}, {}
            create_defaults(ex)
            st.end_recovery()
            ex.dirty = True
            ok = ex.save(force=True)
        return {"ok": True, "saved": ok, "accounts": len(ex.accounts)}


def _cash(a: dict) -> str:
    from decimal import Decimal
    total = Decimal(0)
    for r in a["ledger"]:
        for acct, v in r["postings"]:
            if acct == "cash":
                total += Decimal(str(v))
    return str(total)
