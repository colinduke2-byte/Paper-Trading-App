"""Wash sales, worked out lot by lot. Informational only: nothing here changes the ledger.

The rule as applied here (a simplified reading of the US rule, for practice):

* A closed lot is a loss sale when its gain, after any basis carried into it by an earlier wash
  sale, is negative.
* Replacement shares are other openings of the same symbol in the same direction (another
  purchase for a long lot, another short sale for a short lot) dated within 30 calendar days
  before or after the sale (New York dates). Shares opened before the sale count only while they
  are still held at the sale: shares sold earlier, or in the same sale, are not replacements.
  Each replacement share is used once, earliest acquisition first.
* For the replaced quantity the loss is disallowed (pro rata to quantity, in cents). The
  disallowed amount is added to the replacement shares' cost basis, and the sold lot's holding
  period is added to theirs (their holding period starts earlier by that long).
* When the replacement shares are themselves closed, the carried amount increases their cost, so
  a later loss on them is larger (and can itself be a wash sale), or a gain is smaller.

Not covered: substantially identical securities other than the same symbol (an option and its
stock, for example), purchases in other accounts, and splits after a wash sale (the carried
amounts are not rescaled). Lots are identified by lot id; entries saved before lot ids existed
use the opening fill id, or the opening time.
"""
from bisect import bisect_left, bisect_right
from decimal import Decimal
from typing import Optional

from ..core.money import ZERO, cents
from ..options.symbology import multiplier
from .common import anniversary, ny_date

WINDOW_DAYS = 30
NOTE = ("Wash-sale detail is informational only and is not tax advice. A loss is matched with "
        "purchases of the same symbol (short sales, for a short lot) within 30 days before or after the "
        "sale, earliest first, each replacement share once; the disallowed loss is added to the replacement "
        "lot's basis and the sold lot's holding period to its holding period. Substantially identical "
        "securities other than the same symbol, other accounts and later splits are not considered. "
        "Realised gains in the ledger are never changed.")


def lot_key(row: dict) -> str:
    """Identity of the lot a closed row (or acquisition) belongs to."""
    if row.get("lot_id"):
        return row["lot_id"]
    if row.get("open_fill_id"):
        return "fill:" + row["open_fill_id"]
    return f"ts:{row['symbol']}:{row['direction']}:{row['opened_at']}"


def _resolve(account, rows: list) -> list:
    """Give rows saved before lot ids existed the id of the open lot they came from, when there is one."""
    by_fill, by_ts = {}, {}
    for sym, lots in account.lots.items():
        for lot in lots:
            if lot.fill_id:
                by_fill[lot.fill_id] = lot.id
            else:
                by_ts[(sym, lot.qty > 0, float(lot.ts))] = lot.id
    for r in rows:
        if r.get("lot_id") and r["open_fill_id"]:
            by_fill.setdefault(r["open_fill_id"], r["lot_id"])
    out = []
    for r in rows:
        if not r.get("lot_id"):
            lid = by_fill.get(r["open_fill_id"]) if r["open_fill_id"] else \
                by_ts.get((r["symbol"], r["direction"] == "long", float(r["opened_at"])))
            if lid:
                r = dict(r, lot_id=lid)
        out.append(r)
    return out


def _acquisitions(account, rows: list) -> dict:
    """Every lot ever opened: key -> details, with the quantity it opened (current share units)."""
    acq: dict = {}
    for r in rows:
        k = lot_key(r)
        a = acq.setdefault(k, {"key": k, "symbol": r["symbol"], "direction": r["direction"],
                               "opened_at": r["opened_at"], "fill_id": r["open_fill_id"], "lot_id": r.get("lot_id", ""),
                               "open_price": r["open_price"], "qty": ZERO, "open_qty": ZERO})
        a["qty"] += r["qty"]
    for sym, lots in account.lots.items():
        for lot in lots:
            row = {"lot_id": lot.id, "open_fill_id": lot.fill_id, "symbol": sym,
                   "direction": "long" if lot.qty > 0 else "short", "opened_at": float(lot.ts)}
            k = lot_key(row)
            a = acq.setdefault(k, {"key": k, "symbol": sym, "direction": row["direction"], "opened_at": float(lot.ts),
                                   "fill_id": lot.fill_id, "lot_id": lot.id, "open_price": lot.price,
                                   "qty": ZERO, "open_qty": ZERO})
            a["qty"] += abs(lot.qty)
            a["open_qty"] += abs(lot.qty)
    return acq


def _term(direction: str, opened_at: float, closed_at: float) -> str:
    if direction == "short":
        return "short"
    return "long" if ny_date(closed_at) > anniversary(ny_date(opened_at)) else "short"


def analyse(account, rows: list) -> tuple[list, dict]:
    """Wash-sale detail for every closed row (same order as `rows`) and for the open lots.

    rows: closed_lots(account), in the order they were closed. Returns (per-row detail,
    open-lot adjustments keyed by lot key: {"qty", "amount", "holding_shift", "from"}).
    The result is cached on the account until a lot closes or the open lots change; callers must
    not modify it.
    """
    stamp = None
    if len(rows) == len(account.closed):
        stamp = (len(account.closed), id(account.closed[-1]) if account.closed else None,
                 tuple((l.id, l.qty, l.fill_id, l.ts) for ls in account.lots.values() for l in ls))
        cached = getattr(account, "_wash_cache", None)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    result = _analyse(account, rows)
    if stamp is not None:
        account._wash_cache = (stamp, result)
    return result


def _analyse(account, rows: list) -> tuple[list, dict]:
    """The work behind analyse. Rows are in the order they closed (each fill appends its rows).

    Cost: O(n log n) plus, per loss, the acquisitions in its 61-day window that are still open or
    not yet used up. Acquisitions that are fully closed or fully used as replacements leave the index.
    """
    rows = _resolve(account, rows)
    acq = _acquisitions(account, rows)
    # Index: (symbol, direction) -> acquisitions sorted by (opened_at, key), with New York day numbers.
    index: dict = {}
    for a in sorted(acq.values(), key=lambda a: (a["opened_at"], a["key"])):
        a["day"] = ny_date(a["opened_at"]).toordinal()
        g = index.setdefault((a["symbol"], a["direction"]), ([], [], []))
        g[0].append((a["opened_at"], a["key"]))
        g[1].append(a["day"])
        g[2].append(a["key"])
    # Quantity of each lot closed in each sale (by closing fill) and at each moment, for "sold in the same sale".
    by_fill_total, by_time_total = {}, {}
    for r in rows:
        k = lot_key(r)
        if r["close_fill_id"]:
            by_fill_total[(k, r["close_fill_id"])] = by_fill_total.get((k, r["close_fill_id"]), ZERO) + r["qty"]
        by_time_total[(k, r["closed_at"])] = by_time_total.get((k, r["closed_at"]), ZERO) + r["qty"]
    by_fill_seen, by_time_seen = {}, {}
    closed_before: dict = {}  # key -> quantity closed by earlier rows
    used: dict = {}  # acquisition key -> quantity already used as a replacement
    carrying: dict = {}  # acquisition key -> quantity carrying an unconsumed adjustment
    chunks: dict = {}  # acquisition key -> [{"qty", "amount", "shift", "from"}] carried in, oldest first
    out = []

    def drop(a) -> None:
        g = index[(a["symbol"], a["direction"])]
        i = bisect_left(g[0], (a["opened_at"], a["key"]))
        if i < len(g[0]) and g[2][i] == a["key"]:
            for col in g:
                del col[i]

    def held_at(key: str, r: dict) -> Decimal:
        """Quantity of acquisition `key` still held when row r closed (not sold before or in that sale)."""
        if r["close_fill_id"]:
            k = (key, r["close_fill_id"])
            later = by_fill_total.get(k, ZERO) - by_fill_seen.get(k, ZERO)
        else:
            k = (key, r["closed_at"])
            later = by_time_total.get(k, ZERO) - by_time_seen.get(k, ZERO)
        return acq[key]["qty"] - closed_before.get(key, ZERO) - later

    for r in rows:
        key, q = lot_key(r), r["qty"]
        # 1. basis and holding period carried into this lot by earlier wash sales
        carried, shift, need, took = ZERO, None, q, []
        pending = chunks.get(key, [])
        while need > 0 and pending:
            c = pending[0]
            take = min(need, c["qty"])
            amt = c["amount"] if take == c["qty"] else cents(c["amount"] * take / c["qty"])
            carried += amt
            shift = c["shift"] if shift is None else min(shift, c["shift"])
            took.append(c["from"])
            c["qty"] -= take
            c["amount"] -= amt
            carrying[key] -= take
            need -= take
            if c["qty"] <= 0:
                pending.pop(0)
        adj_open = r["opened_at"] - shift if shift is not None else r["opened_at"]
        base_gain = r["gain"] - carried
        detail = {"basis_adjustment": carried, "adjusted_cost": r["cost"] + carried, "adjusted_opened_at": adj_open,
                  "adjusted_term": _term(r["direction"], adj_open, r["closed_at"]), "carried_from": took,
                  "disallowed": ZERO, "replacements": [], "loss_before_wash": -base_gain if base_gain < 0 else ZERO}
        # 2. a loss: find replacement shares, earliest acquisition first, within 30 days either side
        if base_gain < 0:
            loss, close_day = -base_gain, ny_date(r["closed_at"]).toordinal()
            left, matched, given, reps = q, ZERO, ZERO, []
            g = index.get((r["symbol"], r["direction"]))
            if g is not None:
                lo, hi = bisect_left(g[1], close_day - WINDOW_DAYS), bisect_right(g[1], close_day + WINDOW_DAYS)
                for akey in g[2][lo:hi]:
                    if left <= 0:
                        break
                    if akey == key:
                        continue
                    a = acq[akey]
                    if a["opened_at"] <= r["closed_at"]:
                        # Shares still held, less those already carrying an earlier wash sale's loss.
                        cap = min(held_at(akey, r) - carrying.get(akey, ZERO), a["qty"] - used.get(akey, ZERO))
                    else:
                        cap = a["qty"] - used.get(akey, ZERO)
                    if cap <= 0:
                        continue
                    m = min(left, cap)
                    left -= m
                    matched += m
                    amt = loss - given if matched == q else cents(loss * matched / q) - given
                    given += amt
                    used[akey] = used.get(akey, ZERO) + m
                    holding = r["closed_at"] - adj_open
                    chunks.setdefault(akey, []).append({"qty": m, "amount": amt, "shift": holding, "from": r["id"]})
                    carrying[akey] = carrying.get(akey, ZERO) + m
                    before = cents(m * a["open_price"] * multiplier(a["symbol"]))
                    rep_open = a["opened_at"] - holding
                    reps.append({"key": akey, "lot_id": a["lot_id"], "fill_id": a["fill_id"], "opened_at": a["opened_at"],
                                 "open_date": ny_date(a["opened_at"]).isoformat(), "qty": m, "disallowed": amt,
                                 "basis_before": before, "basis_after": before + amt,
                                 "adjusted_opened_at": rep_open, "adjusted_open_date": ny_date(rep_open).isoformat(),
                                 "status": "open" if a["open_qty"] > 0 else "closed"})
                    if used[akey] >= a["qty"]:
                        drop(a)
            detail["disallowed"] = given
            detail["replacements"] = reps
        detail["adjusted_gain"] = base_gain + detail["disallowed"]
        out.append(detail)
        # 3. this row is now sold: move the running totals on
        closed_before[key] = closed_before.get(key, ZERO) + q
        if r["close_fill_id"]:
            k = (key, r["close_fill_id"])
            by_fill_seen[k] = by_fill_seen.get(k, ZERO) + q
        k = (key, r["closed_at"])
        by_time_seen[k] = by_time_seen.get(k, ZERO) + q
        a = acq[key]
        if a["open_qty"] == 0 and closed_before[key] >= a["qty"]:
            drop(a)  # fully sold: never a replacement for a later sale
    open_adj = {}
    for key, pending in chunks.items():
        pending = [c for c in pending if c["qty"] > 0]
        if pending:
            open_adj[key] = {"qty": sum((c["qty"] for c in pending), ZERO),
                             "amount": sum((c["amount"] for c in pending), ZERO),
                             "holding_shift": min(c["shift"] for c in pending), "from": [c["from"] for c in pending]}
    return out, open_adj


def open_lot_adjustment(open_adj: dict, lot) -> Optional[dict]:
    """The wash-sale adjustment an open lot carries, or None."""
    for key in (lot.id, "fill:" + lot.fill_id if lot.fill_id else None):
        if key and key in open_adj:
            return open_adj[key]
    return None
