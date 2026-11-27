"""Backup archives (zip) and single-account export files: building them, and reading untrusted uploads safely.

A backup archive holds everything of one data mode except sign-ins and secrets:

    manifest.json   {"format": "paperdesk-backup", "format_version": 1, "schema_version", "app_version",
                     "created", "created_ts", "kind", "feed", "simulated", "accounts", "files": {name: {"sha256", "size"}}}
    state.json      the logical state document (self-contained: no segment references)
    market.json     the feed's saved market (optional)
    sessions/<file> recorded replay sessions (optional; manual archives only)

Reading checks, before anything is used: overall size, member count, member names (no absolute paths, no "..",
no backslashes, no directories or links, only the names above), encryption, per-member and total uncompressed
size and compression ratio (decompression bombs), the real number of bytes read against the declared sizes,
every checksum against the manifest, then the state document's version, migration, structure and ledger
invariants. Nothing is written by this module.
"""
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from . import fsutil, schema
from .store import SEG_KEY, encode

FORMAT, FORMAT_VERSION = "paperdesk-backup", 1
ACCOUNT_FORMAT, ACCOUNT_FORMAT_VERSION = "paperdesk-account", 1
MAX_UPLOAD = 64 * 1024 * 1024          # compressed archive
MAX_ACCOUNT_UPLOAD = 32 * 1024 * 1024  # one exported account (JSON)
MAX_MEMBER = 256 * 1024 * 1024         # uncompressed, per file
MAX_TOTAL = 512 * 1024 * 1024          # uncompressed, whole archive
MAX_RATIO = 100                        # uncompressed / compressed, for members above RATIO_FLOOR
RATIO_FLOOR = 1024 * 1024
MAX_MEMBERS = 200
MAX_JSON = 64 * 1024 * 1024           # uncompressed manifest.json, state.json, market.json: parsed in memory
# Session files: no path parts, no trailing dot, and none of the names Windows reserves for devices.
_NAME = re.compile(r"^(manifest\.json|state\.json|market\.json|"
                   r"sessions/(?!(?i:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$))[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?<!\.))$")


class ArchiveError(ValueError):
    """The upload is not a usable PaperDesk backup; the message says why, in plain words."""


@dataclass
class Archive:
    manifest: dict
    doc: dict
    from_version: int
    market: Optional[dict] = None
    sessions: dict = field(default_factory=dict)  # file name -> bytes
    accounts: list = field(default_factory=list)  # loaded Account objects (checked)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def build(doc: dict, market: Optional[dict] = None, sessions: Optional[dict] = None, info: Optional[dict] = None,
          raw: Optional[dict] = None) -> bytes:
    """Build an archive. `raw` (name -> bytes) stores files exactly as given instead of encoding `doc`
    (used for the copy kept before a migration)."""
    files = {}
    if raw is not None:
        files.update(raw)
    else:
        files["state.json"] = encode(doc)
        if market:
            files["market.json"] = encode(market)
    for name, data in (sessions or {}).items():
        files[f"sessions/{name}"] = data
    for name in files:
        if not _NAME.match(name):
            raise ValueError(f"refusing to archive a file named {name!r}")
    info = dict(info or {})
    created = info.pop("created_ts")
    manifest = {"format": FORMAT, "format_version": FORMAT_VERSION, "created": fsutil.iso(created),
                "created_ts": created, **info,
                "files": {n: {"sha256": _sha(b), "size": len(b)} for n, b in files.items()}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=1))
        for n, b in files.items():
            zf.writestr(n, b)
    return buf.getvalue()


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, budget: list) -> bytes:
    cap = MAX_MEMBER if info.filename.startswith("sessions/") else MAX_JSON
    limit = min(info.file_size, cap, budget[0])
    out, total = [], 0
    with zf.open(info) as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ArchiveError(f"{info.filename} is larger than the archive says or than PaperDesk accepts")
            out.append(chunk)
    budget[0] -= total
    return b"".join(out)


def read(data: bytes, max_upload: int = MAX_UPLOAD) -> Archive:
    if len(data) > max_upload:
        raise ArchiveError(f"the file is {len(data) // (1024 * 1024)} MB; backups up to {max_upload // (1024 * 1024)} MB are accepted")
    if not data.startswith(b"PK"):
        raise ArchiveError("the file is not a PaperDesk backup (it is not a zip archive)")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError) as e:
        raise ArchiveError(f"the zip archive is damaged ({e})") from None
    if len(infos) > MAX_MEMBERS:
        raise ArchiveError(f"the archive holds {len(infos)} files; a PaperDesk backup holds far fewer")
    seen, declared = set(), 0
    for i in infos:
        n = i.filename
        if n in seen:
            raise ArchiveError(f"the archive holds {n!r} twice")
        seen.add(n)
        if not _NAME.match(n) or ".." in n:
            raise ArchiveError(f"the archive holds a file PaperDesk does not use or cannot place safely: {n[:80]!r}")
        if i.is_dir() or ((i.external_attr >> 16) & 0o170000) == 0o120000:
            raise ArchiveError(f"the archive holds a folder or link named {n!r}")
        if i.flag_bits & 0x1:
            raise ArchiveError("the archive is encrypted; PaperDesk backups never are")
        if i.file_size > (MAX_MEMBER if n.startswith("sessions/") else MAX_JSON):
            raise ArchiveError(f"{n} would unpack to {i.file_size // (1024 * 1024)} MB, more than PaperDesk accepts")
        if i.file_size > RATIO_FLOOR and i.file_size > max(i.compress_size, 1) * MAX_RATIO:
            raise ArchiveError(f"{n} is compressed suspiciously well (possible zip bomb), so it was not unpacked")
        declared += i.file_size
    if declared > MAX_TOTAL:
        raise ArchiveError("the archive would unpack to more than PaperDesk accepts")
    if "manifest.json" not in seen or "state.json" not in seen:
        raise ArchiveError("the archive is not a PaperDesk backup (manifest.json or state.json is missing)")
    budget = [MAX_TOTAL]
    try:
        files = {i.filename: _read_member(zf, i, budget) for i in infos}
    except (zipfile.BadZipFile, OSError, EOFError, ValueError, NotImplementedError) as e:
        if isinstance(e, ArchiveError):
            raise
        raise ArchiveError(f"the zip archive is damaged ({e})") from None
    try:
        manifest = json.loads(files.pop("manifest.json").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ArchiveError("manifest.json in the archive is damaged") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ArchiveError("the archive is not a PaperDesk backup (its manifest has the wrong format)")
    fv = manifest.get("format_version")
    if not isinstance(fv, int) or fv > FORMAT_VERSION:
        raise ArchiveError("the archive was made by a newer PaperDesk; update PaperDesk to restore it")
    listed = manifest.get("files")
    if not isinstance(listed, dict) or set(listed) != set(files):
        raise ArchiveError("the files in the archive do not match its manifest")
    for n, b in files.items():
        meta = listed[n]
        if not isinstance(meta, dict) or meta.get("sha256") != _sha(b) or meta.get("size") != len(b):
            raise ArchiveError(f"{n} in the archive is damaged (its checksum does not match the manifest)")
    try:
        doc = json.loads(files["state.json"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ArchiveError("state.json in the archive is not valid JSON") from None
    doc, start, accounts = check_document(doc)
    market = None
    if "market.json" in files:
        try:
            market = json.loads(files["market.json"].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ArchiveError("market.json in the archive is not valid JSON") from None
        if not isinstance(market, dict):
            raise ArchiveError("market.json in the archive is damaged")
    sessions = {n.split("/", 1)[1]: b for n, b in files.items() if n.startswith("sessions/")}
    return Archive(manifest, doc, start, market, sessions, accounts)


def _no_segments(obj, depth=0) -> None:
    if depth > 3:
        return
    if isinstance(obj, dict):
        if SEG_KEY in obj:
            raise ArchiveError("the state in the archive refers to files outside it")
        for v in obj.values():
            _no_segments(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:1000]:
            if isinstance(v, (dict, list)):
                _no_segments(v, depth + 1)


def check_document(doc) -> tuple:
    """Migrate, validate and test-load a document from outside. Returns (doc, from_version, accounts)."""
    from ..core.models import Order, from_plain
    from ..ledger.account import Account
    if not isinstance(doc, dict):
        raise ArchiveError("the state in the archive is not a PaperDesk state")
    _no_segments(doc.get("accounts"))
    if isinstance(doc.get("orders"), list) and any(isinstance(o, str) for o in doc["orders"]):
        raise ArchiveError("the state in the archive refers to files outside it")
    try:
        doc, start = schema.migrate(doc)
        schema.validate(doc)
    except schema.NewerVersionError as e:
        raise ArchiveError(f"the backup {e}; update PaperDesk to restore it") from None
    except schema.StateFormatError as e:
        raise ArchiveError(f"the state in the archive is not valid: {e}") from None
    try:
        accounts = [Account.from_plain(a) for a in doc["accounts"]]
        for o in doc["orders"]:
            from_plain(Order, o)
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError, IndexError) as e:
        raise ArchiveError(f"the state in the archive cannot be loaded ({type(e).__name__}: {e})") from None
    problems = [p for a in accounts for p in schema.account_problems(a)]
    if problems:
        raise ArchiveError("the state in the archive is inconsistent: " + "; ".join(problems[:3]))
    return doc, start, accounts


# ---- dry-run summary --------------------------------------------------------------------------------

def _acct_brief(a) -> dict:
    return {"id": a.id, "name": a.name, "type": a.type, "cash": str(a.cash), "positions": len(a.positions()),
            "fills": len(a.fills), "transactions": len(a.ledger.transactions)}


def summarize(ex, arc: Archive, known_symbols=None) -> dict:
    """What a restore would change, account by account."""
    cur = {a.id: _acct_brief(a) for a in ex.accounts.values()}
    new = {a.id: _acct_brief(a) for a in arc.accounts}
    rows = []
    for aid in sorted(set(cur) | set(new)):
        c, n = cur.get(aid), new.get(aid)
        if c and not n:
            rows.append({"id": aid, "change": "removed", "name": c["name"], "before": c, "after": None})
        elif n and not c:
            rows.append({"id": aid, "change": "added", "name": n["name"], "before": None, "after": n})
        else:
            same = {k: v for k, v in c.items()} == {k: v for k, v in n.items()}
            rows.append({"id": aid, "change": "unchanged" if same else "replaced", "name": n["name"], "before": c, "after": n})
    missing = []
    if known_symbols is not None:
        from ..options.symbology import parse
        syms = set()
        for a in arc.accounts:
            syms.update(a.positions())
        for o in arc.doc["orders"]:
            if o.get("status") in ("held", "working", "partially_filled"):
                syms.add(o["symbol"])
        for s in syms:
            c = parse(s)
            base = c.underlying if c else s
            if base not in known_symbols:
                missing.append(s)
    active = sum(1 for o in arc.doc["orders"] if o.get("status") in ("held", "working", "partially_filled"))
    m = arc.manifest
    return {"created": m.get("created"), "kind": m.get("kind"), "app_version": m.get("app_version"),
            "schema_version": m.get("schema_version"), "migrated_from": arc.from_version if arc.from_version != schema.CURRENT_VERSION else None,
            "feed": m.get("feed"), "simulated": m.get("simulated"), "accounts": rows,
            "orders": {"before": sum(1 for o in ex.engine.orders.values() if o.active), "after": active},
            "watchlists": {"before": len(ex.watchlists), "after": len(arc.doc["watchlists"])},
            "alerts": {"before": len(ex.alerts), "after": len(arc.doc["alerts"])},
            "market": arc.market is not None, "sessions": sorted(arc.sessions),
            "missing_symbols": sorted(missing)}


# ---- one account ------------------------------------------------------------------------------------

def export_account(ex, account_id: str, info: dict) -> bytes:
    from ..core.models import to_plain
    a = ex.account(account_id)
    body = {"account": a.to_plain(), "orders": [to_plain(o) for o in ex.engine.orders.values() if o.account_id == a.id]}
    payload = encode(body)
    out = {"format": ACCOUNT_FORMAT, "format_version": ACCOUNT_FORMAT_VERSION, "schema_version": schema.CURRENT_VERSION,
           **info, "sha256": _sha(payload), **body}
    return json.dumps(out, indent=None, separators=(",", ":")).encode("ascii")


@dataclass
class AccountImport:
    source: dict
    account: dict           # remapped plain account (current version)
    orders: list            # remapped plain orders
    old_id: str
    new_id: str
    missing_symbols: list
    canceled: list
    next_seq: dict = field(default_factory=dict)  # counters after the import: account, order, fill, lifecycle


def read_account(data: bytes) -> dict:
    if len(data) > MAX_ACCOUNT_UPLOAD:
        raise ArchiveError(f"the file is larger than {MAX_ACCOUNT_UPLOAD // (1024 * 1024)} MB")
    try:
        d = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ArchiveError("the file is not a PaperDesk account export (it is not valid JSON)") from None
    if not isinstance(d, dict) or d.get("format") != ACCOUNT_FORMAT:
        raise ArchiveError("the file is not a PaperDesk account export")
    if not isinstance(d.get("format_version"), int) or d["format_version"] > ACCOUNT_FORMAT_VERSION:
        raise ArchiveError("the account was exported by a newer PaperDesk; update PaperDesk to import it")
    if not isinstance(d.get("account"), dict) or not isinstance(d.get("orders"), list):
        raise ArchiveError("the account export is incomplete")
    if d.get("sha256") != _sha(encode({"account": d["account"], "orders": d["orders"]})):
        raise ArchiveError("the account export is damaged (its checksum does not match)")
    return d


def prepare_import(ex, d: dict) -> AccountImport:
    """Check an exported account against this exchange and give it new ids. Changes nothing."""
    v = d.get("schema_version", 1)
    doc = {"version": v if isinstance(v, int) else 1, "accounts": [d["account"]], "orders": d["orders"],
           "watchlists": {}, "alerts": [], "seq": {"order": 1, "account": 1, "alert": 1, "fill": 1, "expiry": 1},
           "lifecycle": {"last_date": None, "applied": [], "seq": 1, "splits": []}}
    doc, _, _ = check_document(json.loads(json.dumps(doc)))
    acct, orders = doc["accounts"][0], doc["orders"]
    old_id = acct["id"]
    if any(o["account_id"] != old_id for o in orders):
        raise ArchiveError("the account export holds orders of another account")
    seq = ex._peek_sequences()
    new_id = f"A{seq['account']:04d}"
    # Every order id and fill id gets a new number from this exchange's counters, so nothing collides.
    ids = {old_id: new_id}
    n_order = seq["order"]
    for o in orders:
        ids[o["id"]] = f"O{n_order:08d}"
        n_order += 1
    n_fill, n_life = seq["fill"], ex.lifecycle.to_plain()["seq"]
    for f in acct["fills"]:
        if f["id"] in ids:
            continue
        if f["id"].startswith("L"):  # fills booked by the lifecycle (splits, assignments) have their own counter
            ids[f["id"]] = f"L{n_life:08d}"
            n_life += 1
        else:
            ids[f["id"]] = f"F{n_fill:08d}"
            n_fill += 1
    remap = lambda x: _remap(x, ids)
    acct, orders = remap(acct), remap(orders)
    known = set(ex.feed.symbols())
    from ..options.symbology import parse
    def exists(s):
        c = parse(s)
        return (c.underlying if c else s) in known
    syms = set(acct["lots"]) | {o["symbol"] for o in orders if o["status"] in ("held", "working", "partially_filled")}
    missing = sorted(s for s in syms if not exists(s))
    canceled = []
    for o in orders:
        if o["status"] in ("held", "working", "partially_filled") and not exists(o["symbol"]):
            o["status"], o["reason"] = "canceled", "canceled on import: the symbol does not exist in this feed"
            canceled.append(o["id"])
    return AccountImport(source={k: d.get(k) for k in ("app_version", "created", "feed", "simulated")},
                         account=acct, orders=orders, old_id=old_id, new_id=new_id, missing_symbols=missing,
                         canceled=canceled, next_seq={"account": seq["account"] + 1, "order": n_order,
                                                      "fill": n_fill, "lifecycle": n_life})


def apply_import(ex, imp: AccountImport, name: Optional[str] = None) -> str:
    """Add a prepared account to the exchange. Returns the new account id."""
    from itertools import count
    from ..core.models import Order, from_plain
    from ..ledger.account import Account
    a = Account.from_plain(imp.account)
    wanted = (name or a.name or "Imported account").strip()[:40] or "Imported account"
    names = {x.name for x in ex.accounts.values()}
    if wanted in names:
        wanted = (wanted[:29] + " (imported)") if f"{wanted[:29]} (imported)" not in names else f"{wanted[:30]} {a.id}"
    a.name = wanted
    orders = [from_plain(Order, o) for o in imp.orders]
    if a.id in ex.accounts or any(o.id in ex.engine.orders for o in orders):
        raise ArchiveError("the ids picked for the import are taken; preview the import again")
    ex.accounts[a.id] = a
    for o in orders:
        ex.engine.orders[o.id] = o
    n = imp.next_seq
    ex._acct_seq, ex._order_seq = count(n["account"]), count(n["order"])
    ex.engine._fill_seq = count(n["fill"])
    ex.lifecycle._seq = count(n["lifecycle"])
    ex.dirty = True
    return a.id


def _remap(x, ids: dict):
    """Replace every string that is exactly an old id (account, order or fill id) with its new id."""
    if isinstance(x, str):
        return ids.get(x, x)
    if isinstance(x, list):
        return [_remap(v, ids) for v in x]
    if isinstance(x, dict):
        return {k: _remap(v, ids) for k, v in x.items()}
    return x
