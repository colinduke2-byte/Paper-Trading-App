"""Crash-safe saving and careful loading of one data directory (state.json, market.json, segments/).

Saving is split in two so that it fits a server that serialises under its exchange lock and writes outside it:

    plan = store.plan(ex, ...)   # pure: reads the exchange, returns bytes; call it under the exchange lock
    store.write(plan)            # file I/O only; safe to call outside the lock, from any thread

Write amplification: the big lists only ever grow (ledger transactions, fills, closed trades, and orders once
they are finished), so they are kept in append-only segment files and state.json refers to them by name and
count. A save appends the new entries (fsync'd) and then replaces the small state.json atomically, so a kill
at any point leaves the previous state.json, whose counts only ever refer to entries that were already on
disk. Segments are rewritten in full ("a new generation") when a list is replaced (account reset, restore,
import) and at every automatic backup (compaction). Garbage left by a crash is ignored when loading and
removed after the next good save. docs/STATE_FORMAT.md has the details.

Wall-clock time here (status times, retry back-off) is about real elapsed time, not the simulated market,
and is injected as ``wall`` so tests can control it.
"""
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import fsutil, schema

log = logging.getLogger("paperdesk.storage")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("paperdesk storage: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

STATE, MARKET, SEGMENTS, RECOVERY = "state.json", "market.json", "segments", "recovery"
LISTS = ("ledger", "fills", "closed")
SEG_KEY = "$segment"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_SEG_NAME = re.compile(r"^segments/[A-Za-z0-9_-]{1,60}\.(ledger|fills|closed|orders)\.jsonl$")


def encode(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")


class SaveBlocked(RuntimeError):
    """Saving is switched off because the data on disk must not be overwritten (recovery)."""


@dataclass
class SegmentWrite:
    name: str               # path relative to the data directory, always with forward slashes
    base: int               # entries the file must already hold before `lines` (lists); 0 for a new file
    lines: list             # encoded entries, each ending with b"\n"
    keys: Optional[list] = None  # orders segment: the order id of each line (appended only once per id)
    fresh: bool = False     # a new generation: the file is created from nothing


@dataclass
class SavePlan:
    seq: int
    state: Optional[bytes] = None
    market: Optional[bytes] = None
    segments: list = field(default_factory=list)
    referenced: set = field(default_factory=set)


@dataclass
class LoadResult:
    status: str                      # "empty" (no state yet), "ok", "damaged" or "newer"
    doc: Optional[dict] = None       # the logical document, migrated to the current version
    from_version: Optional[int] = None
    reason: str = ""
    market: Optional[dict] = None
    market_problem: str = ""


class StateStore:
    def __init__(self, data_dir: str, wall: Callable[[], float] = time.time):
        self.dir = data_dir
        self.wall = wall
        self._wlock = threading.Lock()   # one writer at a time
        self._meta = threading.Lock()    # committed segment positions (read by plan, written by write)
        self._seq = 0
        self._written_seq = 0
        self._segs: dict = {}            # name -> [entries, byte offset] committed on disk
        self._order_ids: dict = {}       # orders segment name -> set of order ids committed in it
        self._gens: dict = {}            # account id -> {"token", "objs": (ledger, fills, closed)}
        self._loaded_tokens: dict = {}   # account id -> token found in state.json at load
        self._orders_token: Optional[str] = None
        self._compact = False
        self.recovery: Optional[dict] = None
        self.warnings: list = []
        self.last_save: Optional[float] = None
        self.last_attempt: Optional[float] = None
        self.error: Optional[str] = None
        self.failing_since: Optional[float] = None
        self.attempts = 0
        self.retry_at = 0.0
        self.last_bytes = {"state": 0, "appended": 0, "market": 0}
        self.last_ms = 0.0
        self.saves = 0
        self._last_state: Optional[bytes] = None  # bytes of the state.json on disk, when this process wrote it

    # ---- paths -------------------------------------------------------------------------------
    def path(self, name: str) -> str:
        return os.path.join(self.dir, *name.split("/"))

    # ---- status ------------------------------------------------------------------------------
    @property
    def blocked(self) -> bool:
        return self.recovery is not None

    def status(self) -> dict:
        return {"last_save": self.last_save, "last_attempt": self.last_attempt, "saving_failed": self.error is not None,
                "error": self.error, "failing_since": self.failing_since, "attempts": self.attempts,
                "next_retry": self.retry_at if self.error else None, "recovery": self.recovery,
                "warnings": list(self.warnings), "last_save_ms": round(self.last_ms, 1), "saves": self.saves,
                "last_bytes": dict(self.last_bytes)}

    def should_wait(self, force: bool = False) -> bool:
        """True while saving is blocked, or during the back-off after a failed save (unless forced)."""
        if self.blocked:
            return True
        return bool(self.error) and not force and self.wall() < self.retry_at

    def compact_next(self) -> None:
        """Rewrite every segment from memory at the next save (periodic compaction, after a restore)."""
        with self._meta:
            self._compact = True

    # ---- planning (pure: reads the exchange, touches no file) ------------------------------------
    def _token(self) -> str:
        return secrets.token_hex(4)

    def _seg_name(self, owner: str, token: str, kind: str) -> str:
        owner = owner if _SAFE_ID.match(owner) else "x" + secrets.token_hex(4)
        return f"{SEGMENTS}/{owner}-{token}.{kind}.jsonl"

    def plan(self, accounts: list, orders: list, head: dict, market: Optional[dict] = None,
             app_version: str = "") -> SavePlan:
        """Build the bytes for one save. `accounts` are Account objects, `orders` Order objects in engine
        order, `head` the rest of the document (watchlists, alerts, seq, lifecycle)."""
        from ..core.models import to_plain
        with self._meta:
            self._seq += 1
            plan = SavePlan(self._seq)
            compact, self._compact = self._compact, False
            segs = {k: list(v) for k, v in self._segs.items()}
            if compact or self._orders_token is None:
                self._orders_token = self._token()
            orders_name = self._seg_name("orders", self._orders_token, "orders")
            committed_ids = set() if compact else set(self._order_ids.get(orders_name, ()))
            gens = self._gens
            if compact:
                gens.clear()
                self._loaded_tokens.clear()
        acct_rows = []
        live_ids = set()
        for a in accounts:
            live_ids.add(a.id)
            objs = (a.ledger, a.fills, a.closed)
            g = gens.get(a.id)
            if g is not None and not all(x is y for x, y in zip(g["objs"], objs)):
                g = None  # the account's lists were replaced (reset, restore, import): a new generation
            if g is None:
                g = gens[a.id] = {"token": self._token(), "objs": objs}
            row = a.to_plain(lists=False)
            sizes = {"ledger": len(a.ledger.transactions), "fills": len(a.fills), "closed": len(a.closed)}
            for kind in LISTS:
                name = self._seg_name(a.id, g["token"], kind)
                have = segs.get(name, [0, 0])[0]
                if have > sizes[kind]:  # a list shrank: never expected, so start this account again
                    g["token"] = self._token()
                    return self._replan_after_shrink(accounts, orders, head, market, app_version)
                if sizes[kind] > have:
                    if kind == "ledger":
                        rows = a.ledger.to_plain(start=have)
                    elif kind == "fills":
                        rows = [to_plain(f) for f in a.fills[have:]]
                    else:
                        rows = a.closed[have:]
                    plan.segments.append(SegmentWrite(name, have, [encode(r) + b"\n" for r in rows],
                                                      fresh=name not in segs))
                if sizes[kind] == 0 and name not in segs:
                    row[kind] = []  # nothing to keep in a file yet
                    continue
                row[kind] = {SEG_KEY: name, "count": sizes[kind]}
                plan.referenced.add(name)
            acct_rows.append(row)
        for k in [k for k in gens if k not in live_ids]:
            gens.pop(k, None)
        order_rows, new_lines, new_keys = [], [], []
        for o in orders:
            if o.active:
                order_rows.append(to_plain(o))
                continue
            if o.id not in committed_ids:
                new_lines.append(encode(to_plain(o)) + b"\n")
                new_keys.append(o.id)
            order_rows.append(o.id)
        if new_lines:
            plan.segments.append(SegmentWrite(orders_name, 0, new_lines, keys=new_keys, fresh=orders_name not in segs))
        doc = {"version": schema.CURRENT_VERSION, "app_version": app_version, "accounts": acct_rows,
               "orders": order_rows}
        if any(isinstance(r, str) for r in order_rows) or orders_name in segs:
            doc["orders_segment"] = {SEG_KEY: orders_name}
            plan.referenced.add(orders_name)
        doc.update(head)
        plan.state = encode(doc)
        if market:
            plan.market = encode(market)
        return plan

    def adopt(self, accounts) -> None:
        """After a load: these Account objects are the ones whose lists the loaded segment files hold."""
        with self._meta:
            for a in accounts:
                token = self._loaded_tokens.pop(a.id, None)
                if token:
                    self._gens[a.id] = {"token": token, "objs": (a.ledger, a.fills, a.closed)}
            self._loaded_tokens.clear()

    def _replan_after_shrink(self, *args):
        log.warning("a saved list got shorter than what is on disk; rewriting that account's segments")
        return self.plan(*args)

    # ---- writing (file I/O only) ------------------------------------------------------------------
    def write(self, plan: Optional[SavePlan]) -> bool:
        """Write a plan. Returns True when it reached the disk. Raises nothing for I/O errors: they are kept
        in the status (saving_failed, error) and the caller saves again later."""
        if plan is None:
            return True
        if self.blocked:
            raise SaveBlocked("saving is off while the data directory is in recovery")
        with self._wlock:
            if plan.seq <= self._written_seq:
                return True  # a newer plan was already written; it holds everything this one has
            t0 = time.perf_counter()
            self.last_attempt = self.wall()
            appended = 0
            try:
                os.makedirs(self.dir, exist_ok=True)
                if plan.segments:
                    os.makedirs(self.path(SEGMENTS), exist_ok=True)
                for seg in plan.segments:
                    appended += self._append(seg)
                if plan.segments:
                    fsutil.fsync_dir(self.path(SEGMENTS))
                same = (plan.state is not None and not plan.segments and self._last_state is not None
                        and plan.state == self._last_state)
                if plan.state is not None and not same:
                    fsutil.atomic_write_bytes(self.path(STATE), plan.state)
                    self._last_state = plan.state
                if plan.market is not None:
                    fsutil.atomic_write_bytes(self.path(MARKET), plan.market)
            except OSError as e:
                self._failed(e)
                return False
            self._written_seq = plan.seq
            self.last_ms = (time.perf_counter() - t0) * 1000
            self.last_bytes = {"state": len(plan.state or b""), "appended": appended, "market": len(plan.market or b"")}
            self.saves += 1
            if self.error:
                log.info("saving works again after %d failed attempt(s)", self.attempts)
            self.error, self.failing_since, self.attempts, self.retry_at = None, None, 0, 0.0
            self.last_save = self.wall()
            if plan.state is not None:
                self._collect(plan.referenced)
            return True

    def _failed(self, e: OSError) -> None:
        now = self.wall()
        reason = _describe_os_error(e)
        if self.error is None:
            log.error("could not save to %s: %s. PaperDesk keeps running and will try again.", self.dir, reason)
            self.failing_since = now
        self.error = reason
        self.attempts += 1
        self.retry_at = now + min(60.0, 2.0 ** min(self.attempts, 6))

    def _append(self, seg: SegmentWrite) -> int:
        path = self.path(seg.name)
        with self._meta:
            have = None if seg.fresh and seg.name not in self._segs else self._segs.get(seg.name)
            ids = self._order_ids.setdefault(seg.name, set()) if seg.keys is not None else None
        if seg.keys is not None:
            lines = [ln for ln, k in zip(seg.lines, seg.keys) if k not in ids]
            keys = [k for k in seg.keys if k not in ids]
        else:
            count = have[0] if have else 0
            skip = count - seg.base
            if skip < 0:
                raise OSError(f"segment {seg.name} is behind the save plan")  # never expected; retried in full
            lines, keys = seg.lines[skip:], None
        if not lines:
            return 0
        data = b"".join(lines)
        if have is None:
            with open(path, "wb") as f:  # a new generation starts from nothing
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            offset = 0
        else:
            offset = have[1]
            with open(path, "r+b") as f:
                f.truncate(offset)  # drop a tail a failed or interrupted append may have left
                f.seek(offset)
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        with self._meta:
            cur = self._segs.get(seg.name, [0, 0])
            self._segs[seg.name] = [(cur[0] if have else 0) + len(lines), offset + len(data)]
            if keys is not None:
                ids.update(keys)
        return len(data)

    def _collect(self, referenced: set) -> None:
        """Delete segment files that the state.json just written no longer refers to."""
        d = self.path(SEGMENTS)
        try:
            names = os.listdir(d)
        except OSError:
            return
        for n in names:
            rel = f"{SEGMENTS}/{n}"
            if rel in referenced or not _SEG_NAME.match(rel):
                continue
            try:
                os.unlink(os.path.join(d, n))
            except OSError:
                continue
            with self._meta:
                self._segs.pop(rel, None)
                self._order_ids.pop(rel, None)

    # ---- loading -----------------------------------------------------------------------------------
    def load(self) -> LoadResult:
        """Read and classify what is on disk. Never raises for damaged data and never writes state.json."""
        self._clean_temp_files()
        market, market_problem = self._read_market()
        path = self.path(STATE)
        try:
            raw = fsutil.read_bytes(path)
        except FileNotFoundError:
            return LoadResult("empty", market=market, market_problem=market_problem)
        except OSError as e:
            return self._damaged(f"state.json could not be read ({_describe_os_error(e)})", market, market_problem)
        if not raw.strip():
            return self._damaged("state.json is empty (the last save was probably cut off)", market, market_problem)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return self._damaged("state.json is not text (it holds bytes that are not UTF-8)", market, market_problem)
        except ValueError as e:
            what = "cut off" if getattr(e, "pos", 0) >= len(raw.rstrip()) - 1 else "corrupted"
            return self._damaged(f"state.json is not valid JSON; it looks {what} ({e})", market, market_problem)
        try:
            version = schema.version_of(doc)
            if version > schema.CURRENT_VERSION:
                raise schema.NewerVersionError(version)
            tokens = self._expand(doc)
            doc, start = schema.migrate(doc)
            schema.validate(doc)
        except schema.NewerVersionError as e:
            res = self._damaged(f"state.json {e}", market, market_problem, status="newer")
            return res
        except schema.StateFormatError as e:
            return self._damaged(f"state.json is not a valid PaperDesk state: {e}", market, market_problem)
        self._loaded_tokens = tokens
        if start < schema.CURRENT_VERSION:
            self._keep_before_migration(raw, start)
        return LoadResult("ok", doc, start, market=market, market_problem=market_problem)

    def _keep_before_migration(self, raw: bytes, start: int) -> None:
        """Keep the old files, byte for byte, as a backup before the first save rewrites them."""
        from .. import __version__
        from .backups import BackupError, BackupManager
        files = {STATE: raw}
        try:
            files[MARKET] = fsutil.read_bytes(self.path(MARKET))
        except OSError:
            pass
        try:
            row = BackupManager(self.dir, self.wall).create(
                "pre-migration", raw=files, info={"schema_version": start, "app_version": __version__,
                                                  "note": f"state.json as it was before conversion from version {start}"})
            log.info("converted state.json from format version %d to %d; the old files are kept in backups/%s",
                     start, schema.CURRENT_VERSION, row["name"])
        except (BackupError, OSError, ValueError) as e:
            self.warnings.append(f"could not keep a copy of the old state before converting it ({e})")

    def _read_market(self):
        try:
            raw = fsutil.read_bytes(self.path(MARKET))
        except FileNotFoundError:
            return None, ""
        except OSError as e:
            return None, f"market.json could not be read ({_describe_os_error(e)})"
        try:
            m = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, "market.json is damaged, so the price history starts again"
        if not isinstance(m, dict):
            return None, "market.json is damaged, so the price history starts again"
        return m, ""

    def _expand(self, doc: dict) -> dict:
        """Replace segment references with the entries they name. Returns {account id: token}."""
        tokens = {}
        accounts = doc.get("accounts")
        if not isinstance(accounts, list):
            return tokens
        segs, order_ids = {}, {}
        for a in accounts:
            if not isinstance(a, dict):
                continue
            for kind in LISTS:
                ref = a.get(kind)
                if not (isinstance(ref, dict) and SEG_KEY in ref):
                    continue
                name, count = ref.get(SEG_KEY), ref.get("count")
                if not (isinstance(name, str) and _SEG_NAME.match(name)) or not isinstance(count, int) or count < 0:
                    raise schema.StateFormatError(f"account {a.get('id')!r} has a bad {kind} segment reference")
                rows, offset = self._read_lines(name, count)
                a[kind] = rows
                segs[name] = [count, offset]
                m = re.match(r"^segments/(.+)-([0-9a-f]+)\.", name)
                if m and m.group(1) == a.get("id"):
                    tokens[a["id"]] = m.group(2)
        ref = doc.pop("orders_segment", None)
        by_id = {}
        if isinstance(ref, dict):
            name = ref.get(SEG_KEY)
            if not (isinstance(name, str) and _SEG_NAME.match(name)):
                raise schema.StateFormatError("the orders segment reference is bad")
            rows, offset = self._read_lines(name, None)
            for r in rows:
                if not (isinstance(r, dict) and isinstance(r.get("id"), str)):
                    raise schema.StateFormatError(f"{name} holds an entry that is not an order")
                by_id[r["id"]] = r
            segs[name] = [len(rows), offset]
            order_ids[name] = set(by_id)
            m = re.match(r"^segments/orders-([0-9a-f]+)\.orders", name)
            self._orders_token = m.group(1) if m else None
        orders = doc.get("orders")
        if isinstance(orders, list):
            out = []
            for o in orders:
                if isinstance(o, str):
                    if o not in by_id:
                        raise schema.StateFormatError(f"order {o} is missing from the orders segment")
                    out.append(by_id[o])
                else:
                    out.append(o)
            doc["orders"] = out
        with self._meta:
            self._segs, self._order_ids = segs, order_ids
            self._gens = {}
        return tokens

    def _read_lines(self, name: str, count: Optional[int]):
        """The first `count` entries of a segment (all complete ones when count is None) and the byte
        offset just after them. A cut-off last line is ignored; anything wrong before it is damage."""
        try:
            raw = fsutil.read_bytes(self.path(name))
        except FileNotFoundError:
            if count == 0:
                return [], 0
            raise schema.StateFormatError(f"{name} is missing") from None
        except OSError as e:
            raise schema.StateFormatError(f"{name} could not be read ({_describe_os_error(e)})") from None
        rows, pos = [], 0
        while count is None or len(rows) < count:
            nl = raw.find(b"\n", pos)
            if nl < 0:
                break
            try:
                rows.append(json.loads(raw[pos:nl]))
            except ValueError:
                raise schema.StateFormatError(f"{name} has a damaged entry at byte {pos}") from None
            pos = nl + 1
        if count is not None and len(rows) < count:
            raise schema.StateFormatError(f"{name} holds {len(rows)} entries but state.json expects {count}")
        return rows, pos

    def _damaged(self, reason: str, market, market_problem, status: str = "damaged") -> LoadResult:
        copy = self.quarantine(reason)
        self.recovery = {"kind": status, "reason": reason, "copy": copy, "detected_at": self.wall()}
        if status == "newer":
            log.error("%s. Nothing will be saved until you choose what to do; the file is left as it is.", reason)
        else:
            log.error("%s. A copy was kept in %s. Nothing will be saved until you choose what to do.", reason, copy)
        return LoadResult(status, reason=reason, market=market, market_problem=market_problem)

    def quarantine(self, reason: str) -> Optional[str]:
        """Copy the current files aside, into recovery/<time>/, before anything can change them."""
        dest = self.path(f"{RECOVERY}/{fsutil.stamp(self.wall())}")
        n, base = 1, dest
        while os.path.exists(dest):
            n += 1
            dest = f"{base}-{n}"
        try:
            os.makedirs(dest)
            for name in (STATE, MARKET):
                src = self.path(name)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(dest, name))
            segdir = self.path(SEGMENTS)
            if os.path.isdir(segdir):
                shutil.copytree(segdir, os.path.join(dest, SEGMENTS))
            with open(os.path.join(dest, "reason.txt"), "w", encoding="utf-8") as f:
                f.write(reason + "\n")
        except OSError as e:
            log.error("could not copy the damaged files aside (%s)", _describe_os_error(e))
            return None
        return dest

    def _clean_temp_files(self) -> None:
        """Remove temporary files a crash in the middle of a save left behind (never anything else)."""
        for d in (self.dir, self.path(SEGMENTS)):
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for n in names:
                if n.startswith(".") and n.endswith(".tmp") and (n.startswith(".state.json.") or n.startswith(".market.json.")):
                    try:
                        os.unlink(os.path.join(d, n))
                    except OSError:
                        pass

    def end_recovery(self) -> None:
        with self._meta:
            self.recovery = None
            self._compact = True


def _describe_os_error(e: OSError) -> str:
    import errno
    code = getattr(e, "errno", None)
    if code == errno.ENOSPC:
        return "the disk is full"
    if code in (errno.EACCES, errno.EPERM) or isinstance(e, PermissionError):
        return "permission denied (the folder or file is read-only, or another program has it open)"
    if code == errno.EROFS:
        return "the disk is read-only"
    return f"{type(e).__name__}: {e.strerror or e}"
