"""The state document: schema version, ordered migrations, validation and ledger invariants.

The *logical* state document is one JSON object (see docs/STATE_FORMAT.md). This module only deals with
that logical form: everything inline, no segment references (store.py expands those first).

Rules for a change to the format (also in docs/STATE_FORMAT.md):
  * A change that an older build could misread bumps CURRENT_VERSION and adds a function to MIGRATIONS
    that turns version N-1 into version N. Migrations are never edited once released; fix forward.
  * Every released format has a fixture in tests/fixtures/states/ and the test suite migrates each one
    to the current version and checks the ledger invariants.
  * A file with a version above CURRENT_VERSION is refused (never overwritten): it was written by a newer
    PaperDesk, and this build cannot know what it would lose.
"""
from dataclasses import MISSING, fields
from decimal import Decimal, InvalidOperation
from typing import Callable

from ..core.models import Fill, Order
from ..lifecycle import settings as lifecycle_settings
from ..lifecycle.state import AccountLife

CURRENT_VERSION = 2
LOT_METHODS = ("fifo", "lifo", "highest_cost")


class StateFormatError(ValueError):
    """The document is not a valid PaperDesk state (wrong type, missing field, bad number)."""


class NewerVersionError(StateFormatError):
    def __init__(self, version):
        self.version = version
        super().__init__(f"it was written by a newer PaperDesk (state format version {version}; this build reads "
                         f"up to version {CURRENT_VERSION})")


def _dataclass_defaults(cls) -> dict:
    out = {}
    for f in fields(cls):
        if f.default is not MISSING:
            v = f.default
            out[f.name] = str(v) if isinstance(v, Decimal) else v
    return out


# ---- migrations ---------------------------------------------------------------------------------

def fill_defaults(d: dict) -> dict:
    """Give every optional field its default. Idempotent; also run on current-version documents, so that a
    hand-edited file with a field left out still loads as before."""
    life_default = AccountLife().to_plain()
    order_defaults, fill_defaults = _dataclass_defaults(Order), _dataclass_defaults(Fill)
    for a in d.get("accounts", []):
        if not isinstance(a, dict):
            continue
        lots = a.get("lots")
        if isinstance(lots, dict):  # lots saved before the opening fill id was recorded have four fields
            for s, ls in lots.items():
                if isinstance(ls, list):
                    lots[s] = [l + [""] if isinstance(l, list) and len(l) == 4 else l for l in ls]
        for r in a.get("ledger") or []:
            if isinstance(r, dict):
                r.setdefault("ref", "")
        for f in a.get("fills") or []:
            if isinstance(f, dict):
                for k, v in fill_defaults.items():
                    f.setdefault(k, v)
        a.setdefault("closed", [])
        a.setdefault("equity_history", [])
        a.setdefault("day_start_equity", a.get("starting_cash"))
        if a.get("lot_method") not in LOT_METHODS:
            a["lot_method"] = "fifo"
        a.setdefault("journal", [])
        a["settings"] = lifecycle_settings.merged(a.get("settings"))
        if not isinstance(a.get("lifecycle"), dict):
            a["lifecycle"] = dict(life_default)
        else:
            a["lifecycle"] = AccountLife.from_plain(a["lifecycle"]).to_plain()
    for o in d.get("orders", []):
        if isinstance(o, dict):
            for k, v in order_defaults.items():
                o.setdefault(k, v)
    d.setdefault("watchlists", {})
    d.setdefault("alerts", [])
    seq = d.get("seq")
    if isinstance(seq, dict):
        for k in ("order", "account", "alert", "fill", "expiry"):
            seq.setdefault(k, 1)
    if not isinstance(d.get("lifecycle"), dict):
        d["lifecycle"] = {"last_date": None, "applied": [], "seq": 1, "splits": []}
    return d


def v1_to_v2(d: dict) -> dict:
    """Version 1 is every file written before versioning was enforced: each build added fields and read the
    missing ones with a default. Version 2 has every field present, so those defaults live here, once."""
    d = fill_defaults(d)
    d["version"] = 2
    return d


# MIGRATIONS[n] turns a version-n document into version n+1. Ordered; never edited after release.
MIGRATIONS: dict[int, Callable[[dict], dict]] = {1: v1_to_v2}


def version_of(d) -> int:
    if not isinstance(d, dict):
        raise StateFormatError("the file does not hold a JSON object")
    v = d.get("version", 1)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise StateFormatError(f"its version field is not a positive whole number ({v!r})")
    return v


def migrate(d: dict) -> tuple:
    """Bring a document up to CURRENT_VERSION in place. Returns (document, the version it was)."""
    start = v = version_of(d)
    if v > CURRENT_VERSION:
        raise NewerVersionError(v)
    while v < CURRENT_VERSION:
        try:
            d = MIGRATIONS[v](d)
        except StateFormatError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation) as e:
            raise StateFormatError(f"it could not be converted from version {v} ({type(e).__name__}: {e})") from e
        v = version_of(d)
    try:
        d = fill_defaults(d)
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation) as e:
        raise StateFormatError(f"a field has the wrong type ({type(e).__name__}: {e})") from e
    return d, start


# ---- validation -----------------------------------------------------------------------------------

def _fail(path: str, what: str):
    raise StateFormatError(f"{path} {what}")


def _num(v, path: str, optional: bool = False):
    if v is None and optional:
        return
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        _fail(path, f"should be a number, not {type(v).__name__}")
    try:
        x = Decimal(str(v))
    except InvalidOperation:
        _fail(path, f"is not a number ({str(v)[:40]!r})")
    if not x.is_finite():
        _fail(path, "is not a finite number")


def _type(v, t, path: str, optional: bool = False):
    if v is None and optional:
        return
    if isinstance(v, bool) and t in (int, float, (int, float)):
        _fail(path, "should be a number, not true/false")
    if not isinstance(v, t):
        name = t.__name__ if isinstance(t, type) else " or ".join(x.__name__ for x in t)
        _fail(path, f"should be {name}, not {type(v).__name__}")


_DEC_ORDER = ("qty", "limit_price", "stop_price", "trail_amount", "trail_percent", "filled_qty", "avg_fill_price",
              "trail_ref", "fees")


def validate_order(o, path: str) -> None:
    _type(o, dict, path)
    for k in ("id", "account_id", "symbol", "side", "type", "status", "tif"):
        _type(o.get(k), str, f"{path}.{k}")
    for k in _DEC_ORDER:
        _num(o.get(k), f"{path}.{k}", optional=k != "qty")
    for k in ("created_at", "updated_at"):
        _type(o.get(k), (int, float), f"{path}.{k}")
    _type(o.get("legs"), list, f"{path}.legs", optional=True)


def validate_account(a, path: str) -> None:
    _type(a, dict, path)
    for k in ("id", "name", "type"):
        _type(a.get(k), str, f"{path}.{k}")
    if a["type"] not in ("cash", "margin"):
        _fail(f"{path}.type", f"should be cash or margin, not {a['type']!r}")
    _num(a.get("starting_cash"), f"{path}.starting_cash")
    _num(a.get("day_start_equity"), f"{path}.day_start_equity")
    _type(a.get("created_at"), (int, float), f"{path}.created_at")
    _type(a.get("ledger"), list, f"{path}.ledger")
    for i, r in enumerate(a["ledger"]):
        p = f"{path}.ledger[{i}]"
        _type(r, dict, p)
        _type(r.get("ts"), (int, float), f"{p}.ts")
        _type(r.get("kind"), str, f"{p}.kind")
        _type(r.get("memo"), str, f"{p}.memo")
        _type(r.get("postings"), list, f"{p}.postings")
        for j, pst in enumerate(r["postings"]):
            if not (isinstance(pst, list) and len(pst) == 2 and isinstance(pst[0], str)):
                _fail(f"{p}.postings[{j}]", "should be [account, amount]")
            _num(pst[1], f"{p}.postings[{j}][1]")
    _type(a.get("lots"), dict, f"{path}.lots")
    for s, ls in a["lots"].items():
        _type(ls, list, f"{path}.lots.{s}")
        for i, l in enumerate(ls):
            p = f"{path}.lots.{s}[{i}]"
            if not (isinstance(l, list) and len(l) == 5):
                _fail(p, "should be [qty, price, time, cost, fill id]")
            _num(l[0], p + "[0]")
            _num(l[1], p + "[1]")
            _type(l[2], (int, float), p + "[2]")
            _num(l[3], p + "[3]")
            _type(l[4], str, p + "[4]")
    _type(a.get("fills"), list, f"{path}.fills")
    for i, f in enumerate(a["fills"]):
        p = f"{path}.fills[{i}]"
        _type(f, dict, p)
        for k in ("id", "account_id", "symbol", "side", "kind"):
            _type(f.get(k), str, f"{p}.{k}")
        for k in ("qty", "price", "fee"):
            _num(f.get(k), f"{p}.{k}")
        _type(f.get("ts"), (int, float), f"{p}.ts")
    for k in ("closed", "equity_history", "journal"):
        _type(a.get(k), list, f"{path}.{k}")
    _type(a.get("settings"), dict, f"{path}.settings")
    _type(a.get("lifecycle"), dict, f"{path}.lifecycle")
    _type(a.get("lot_method"), str, f"{path}.lot_method")


def validate(d: dict) -> None:
    """Structural check of a current-version document. Raises StateFormatError naming the first problem."""
    _type(d, dict, "the document")
    if d.get("version") != CURRENT_VERSION:
        _fail("version", f"should be {CURRENT_VERSION}")
    _type(d.get("accounts"), list, "accounts")
    ids = set()
    for i, a in enumerate(d["accounts"]):
        validate_account(a, f"accounts[{i}]")
        if a["id"] in ids:
            _fail(f"accounts[{i}].id", f"repeats {a['id']!r}")
        ids.add(a["id"])
    _type(d.get("orders"), list, "orders")
    oids = set()
    for i, o in enumerate(d["orders"]):
        validate_order(o, f"orders[{i}]")
        if o["id"] in oids:
            _fail(f"orders[{i}].id", f"repeats {o['id']!r}")
        oids.add(o["id"])
    _type(d.get("watchlists"), dict, "watchlists")
    for k, v in d["watchlists"].items():
        _type(v, list, f"watchlists.{k}")
    _type(d.get("alerts"), list, "alerts")
    _type(d.get("seq"), dict, "seq")
    for k in ("order", "account", "alert", "fill", "expiry"):
        _type(d["seq"].get(k), int, f"seq.{k}")
    _type(d.get("lifecycle"), dict, "lifecycle")


# ---- invariants ----------------------------------------------------------------------------------

def account_problems(account) -> list:
    """Ledger invariants of a loaded Account. Empty when it is consistent."""
    from ..core.money import ZERO
    out = []
    led = account.ledger
    if sum(led.balances.values(), ZERO) != 0:
        out.append(f"account {account.id}: the ledger does not balance")
    recomputed = {k: v for k, v in led.recompute().items() if v}
    if recomputed != {k: v for k, v in led.balances.items() if v}:
        out.append(f"account {account.id}: ledger balances do not match its transactions")
    for sym in account.lots:
        if led.balance(f"pos:{sym}") != account.cost_basis(sym):
            out.append(f"account {account.id}: the cost of {sym} in the ledger does not match its tax lots")
    for k, v in led.balances.items():
        if k.startswith("pos:") and v and k[4:] not in account.lots:
            out.append(f"account {account.id}: the ledger holds a cost for {k[4:]} but there is no position")
    return out
