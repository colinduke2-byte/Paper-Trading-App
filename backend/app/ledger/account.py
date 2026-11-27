"""A paper account: ledger, tax lots, fills and closed-trade history.

Tax lots
    Every open lot has an id ("L1", "L2", ...) that stays with it for its whole life: partial
    closes, splits (quantity and price change, the id does not) and option split adjustments
    (the lot moves to the new contract symbol with its id). A closing fill closes lots either
    in the order the closing order named ("specific" lots, see `select_lots`) or by the
    account's lot method.
"""
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..core.calendar import ny_date
from ..core.models import BUY, Fill, from_plain, to_plain
from ..core.money import ZERO, D, cents
from ..lifecycle import settings as lifecycle_settings
from ..lifecycle.state import AccountLife
from ..options.symbology import describe
from .ledger import CAPITAL, CASH, FEES, REALIZED, Ledger, position_account


@dataclass
class Lot:
    qty: Decimal  # signed: negative for short
    price: Decimal
    ts: float
    cost: Decimal = ZERO  # remaining cost basis in cents (always positive)
    fill_id: str = ""  # the fill that opened the lot ("" for lots saved before this was recorded)
    id: str = ""  # stable lot id, unique within the account ("L1", "L2", ...)


# Which open lot a closing fill consumes first. Ties always go to the oldest lot.
#   fifo / lifo     oldest / newest lot first.
#   highest_cost    the long lot with the highest price per unit (for a short position: the lot
#                   sold at the lowest price), so it realises the smallest gain either way.
#   lowest_cost     the mirror image: the long lot bought cheapest (the short lot sold highest),
#                   realising the largest gain.
#   tax_loss        tax-efficient loss harvesting: the lot with the largest loss per unit at the
#                   fill price first (the smallest gain when nothing is at a loss); on equal
#                   amounts a long-term lot goes before a short-term one.
LOT_METHODS = ("fifo", "lifo", "highest_cost", "lowest_cost", "tax_loss")
SPECIFIC = "specific"  # the lot method recorded on a lot closed because the order named it


def _ny_date(ts: float):
    return ny_date(ts)  # Windows-safe near the epoch (core.calendar.utc_datetime)


def long_term_from(open_ts: float):
    """The first New York date on which a long lot opened at open_ts counts as long-term.

    Held for more than one year: the day after the one-year anniversary (28 February is the
    anniversary of 29 February).
    """
    d = _ny_date(open_ts)
    try:
        anniv = d.replace(year=d.year + 1)
    except ValueError:
        anniv = d.replace(year=d.year + 1, day=28)
    return date.fromordinal(anniv.toordinal() + 1)


def is_long_term(lot: "Lot", ts: float) -> bool:
    return lot.qty > 0 and _ny_date(ts) >= long_term_from(lot.ts)


def pick_lot(lots: list, method: str, price: Decimal | None = None, ts: float | None = None) -> int:
    """Index of the lot to close next. All lots have the same sign (one position)."""
    if method == "lifo":
        return len(lots) - 1
    long = lots[0].qty > 0
    if method == "tax_loss" and price is not None:
        # gain per unit if closed at `price`; smallest first, then long-term, then oldest
        gain = (lambda l: price - l.price) if long else (lambda l: l.price - price)
        when = ts if ts is not None else 0.0
        return min(range(len(lots)), key=lambda i: (gain(lots[i]), not is_long_term(lots[i], when), i))
    if method in ("highest_cost", "tax_loss"):
        if long:
            return max(range(len(lots)), key=lambda i: (lots[i].price, -i))
        return min(range(len(lots)), key=lambda i: (lots[i].price, i))
    if method == "lowest_cost":
        if long:
            return min(range(len(lots)), key=lambda i: (lots[i].price, i))
        return max(range(len(lots)), key=lambda i: (lots[i].price, -i))
    return 0


class Account:
    def __init__(self, id: str, name: str, type: str, starting_cash: Decimal, ts: float):
        if type not in ("cash", "margin"):
            raise ValueError("account type must be 'cash' or 'margin'")
        self.id, self.name, self.type = id, name, type
        self.starting_cash = D(starting_cash)
        self.created_at = ts
        self.ledger = Ledger()
        self.lots: dict[str, list[Lot]] = {}
        self.fills: list[Fill] = []
        self.closed: list[dict] = []
        self.equity_history: list[list] = []  # [ts, equity]
        self.day_start_equity = self.starting_cash
        self.margin_call = False
        self.lot_method = "fifo"
        self.lot_seq = 1  # next lot id number
        self.journal: list[dict] = []  # trade journal entries, managed by the reports routes
        self.settings = lifecycle_settings.defaults()  # per-account rule toggles and rates
        self.life = AccountLife()  # settlement, day trades, accruals, dividends, margin call
        self.ledger.post(ts, "deposit", "Opening balance", [(CASH, self.starting_cash), (CAPITAL, -self.starting_cash)])

    # ---- derived state -------------------------------------------------
    @property
    def cash(self) -> Decimal:
        return self.ledger.balance(CASH)

    @property
    def realized(self) -> Decimal:
        return -self.ledger.balance(REALIZED)

    @property
    def fees_paid(self) -> Decimal:
        return self.ledger.balance(FEES)

    def position(self, symbol: str) -> Decimal:
        return sum((l.qty for l in self.lots.get(symbol, ())), ZERO)

    def positions(self) -> dict:
        return {s: q for s in self.lots if (q := self.position(s)) != 0}

    def avg_cost(self, symbol: str) -> Decimal:
        lots = self.lots.get(symbol, ())
        qty = sum((l.qty for l in lots), ZERO)
        return sum((l.qty * l.price for l in lots), ZERO) / qty if qty else ZERO

    def cost_basis(self, symbol: str) -> Decimal:
        """Signed remaining cost basis (negative for a short position)."""
        return sum((l.cost if l.qty > 0 else -l.cost for l in self.lots.get(symbol, ())), ZERO)

    def new_lot_id(self) -> str:
        lid = f"L{self.lot_seq}"
        self.lot_seq += 1
        return lid

    def find_lot(self, lot_id: str):
        """(symbol, lot) of an open lot, or (None, None)."""
        for sym, lots in self.lots.items():
            for lot in lots:
                if lot.id and lot.id == lot_id:
                    return sym, lot
        return None, None

    def select_lots(self, symbol: str, side: str, qty: Decimal, price: Decimal | None = None,
                    ts: float | None = None, plan: list | None = None, reserved: dict | None = None) -> list:
        """Which open lots a closing trade of `qty` closes, without changing anything.

        Returns [(index into self.lots[symbol], quantity, method, fallback)]. Lots named in
        `plan` ([{"lot_id", "qty", "filled"}], the closing order's choice) close first, each up
        to what is left of its named quantity and of the lot itself. Anything more closes by the
        account's lot method, skipping quantity that other working orders have reserved
        (`reserved`: lot id -> quantity) for as long as unreserved lots remain. A named lot that
        is gone or smaller than named by fill time is therefore replaced by the method
        (`fallback` is True on those rows). Quantity beyond the position opens a new lot.
        """
        lots = self.lots.get(symbol, [])
        sign = 1 if side == BUY else -1
        if not lots or (lots[0].qty > 0) == (sign > 0) or qty <= 0:
            return []
        left = [abs(l.qty) for l in lots]
        reserved = reserved or {}
        out, remaining = [], qty
        for entry in plan or ():
            if remaining <= 0:
                break
            want = D(entry["qty"]) - D(entry.get("filled", "0"))
            idx = next((i for i, l in enumerate(lots) if l.id and l.id == entry["lot_id"]), None)
            if want <= 0 or idx is None or left[idx] <= 0:
                continue
            take = min(remaining, want, left[idx])
            out.append((idx, take, SPECIFIC, False))
            left[idx] -= take
            remaining -= take
        fallback = bool(plan)
        while remaining > 0:
            open_idx = [i for i in range(len(lots)) if left[i] > 0]
            if not open_idx:
                break
            free = [i for i in open_idx if left[i] > reserved.get(lots[i].id, ZERO)]
            cands = free or open_idx
            idx = cands[pick_lot([lots[i] for i in cands], self.lot_method, price, ts)]
            cap = left[idx] - reserved.get(lots[idx].id, ZERO) if free else left[idx]
            take = min(remaining, cap)
            out.append((idx, take, self.lot_method, fallback))
            left[idx] -= take
            remaining -= take
        return out

    # ---- mutations -----------------------------------------------------
    def apply_fill(self, fill: Fill, multiplier: int, plan: list | None = None, reserved: dict | None = None) -> Decimal:
        """Book a fill. Returns the realised profit or loss of any closed quantity.

        `plan` is the closing order's named lots (updated in place: each entry's "filled"
        grows by what this fill closed of it); `reserved` the lot quantities other working
        orders have named. See `select_lots`.
        """
        mult = Decimal(multiplier)
        sign = 1 if fill.side == BUY else -1
        lots = self.lots.setdefault(fill.symbol, [])
        picks = self.select_lots(fill.symbol, fill.side, fill.qty, fill.price, fill.ts, plan, reserved)
        remaining, closed_cost, closed_notional, pnl = fill.qty, ZERO, ZERO, ZERO
        for idx, take, method, fallback in picks:
            lot = lots[idx]
            cost = lot.cost if take == abs(lot.qty) else cents(lot.cost * take / abs(lot.qty))
            lot.cost -= cost
            proceeds = cents(take * fill.price * mult)
            lot_pnl = (proceeds - cost) if lot.qty > 0 else (cost - proceeds)
            closed_cost += cost
            closed_notional += proceeds
            pnl += lot_pnl
            rec = {"symbol": fill.symbol, "qty": str(take if lot.qty > 0 else -take),
                   "open_price": str(lot.price), "close_price": str(fill.price),
                   "pnl": str(lot_pnl), "opened_at": lot.ts, "closed_at": fill.ts,
                   "id": f"C{len(self.closed) + 1}", "basis": str(cost), "close_amount": str(proceeds),
                   "open_fill_id": lot.fill_id, "close_fill_id": fill.id, "order_id": fill.order_id,
                   "kind": fill.kind, "lot_method": method, "lot_id": lot.id}
            if fallback:
                rec["lot_fallback"] = True  # a named lot was no longer there; the method chose this one
            self.closed.append(rec)
            if method == SPECIFIC:
                for entry in plan:
                    if entry["lot_id"] == lot.id:
                        entry["filled"] = str(D(entry.get("filled", "0")) + take)
                        break
            lot.qty += take if lot.qty < 0 else -take
            remaining -= take
        lots[:] = [l for l in lots if l.qty != 0]
        # Closed proceeds are the sum of the per-lot amounts, so profit and cash always agree.
        open_notional = cents(remaining * fill.price * mult)
        if remaining > 0:
            lots.append(Lot(sign * remaining, fill.price, fill.ts, open_notional, fill.id, self.new_lot_id()))
        if not lots:
            self.lots.pop(fill.symbol, None)
        notional = closed_notional + open_notional
        pos = position_account(fill.symbol)
        postings = [(CASH, -sign * notional - fill.fee), (FEES, fill.fee),
                    (pos, sign * open_notional), (pos, sign * closed_cost), (REALIZED, -pnl)]
        memo = f"{fill.kind.capitalize()}: {fill.side} {fill.qty.normalize():f} {describe(fill.symbol)} at {fill.price.normalize():f}"
        self.ledger.post(fill.ts, fill.kind, memo, postings, ref=fill.id)
        self.fills.append(fill)
        return pnl

    # ---- storage -------------------------------------------------------
    def to_plain(self, lists: bool = True) -> dict:
        """Storage form. lists=False leaves out the append-only lists (ledger, fills, closed), which the
        storage layer keeps in segment files (app/storage/store.py)."""
        out = {"id": self.id, "name": self.name, "type": self.type, "starting_cash": str(self.starting_cash),
               "created_at": self.created_at,
               "lots": {s: [[str(l.qty), str(l.price), l.ts, str(l.cost), l.fill_id, l.id] for l in ls]
                        for s, ls in self.lots.items()}, "lot_seq": self.lot_seq,
               "equity_history": self.equity_history, "day_start_equity": str(self.day_start_equity),
               "lot_method": self.lot_method, "journal": self.journal,
               "settings": self.settings, "lifecycle": self.life.to_plain()}
        if lists:
            out.update(ledger=self.ledger.to_plain(), fills=[to_plain(f) for f in self.fills], closed=self.closed)
        return out

    @classmethod
    def from_plain(cls, d: dict) -> "Account":
        a = cls.__new__(cls)
        a.id, a.name, a.type = d["id"], d["name"], d["type"]
        a.starting_cash = D(d["starting_cash"])
        a.created_at = d["created_at"]
        a.ledger = Ledger.from_plain(d["ledger"])
        # Lots saved before the opening fill id was recorded have four fields, before lot ids five.
        a.lots = {s: [Lot(D(l[0]), D(l[1]), l[2], D(l[3]), l[4] if len(l) > 4 else "", l[5] if len(l) > 5 else "")
                      for l in ls] for s, ls in d["lots"].items()}
        used = [int(l.id[1:]) for ls in a.lots.values() for l in ls if re.fullmatch(r"L\d+", l.id or "")]
        used += [int(c["lot_id"][1:]) for c in d.get("closed", []) if re.fullmatch(r"L\d+", c.get("lot_id") or "")]
        a.lot_seq = max(int(d.get("lot_seq", 1)), max(used, default=0) + 1)
        for ls in a.lots.values():  # older files: give every open lot an id, oldest first
            for l in ls:
                if not l.id:
                    l.id = a.new_lot_id()
        a.fills = [from_plain(Fill, f) for f in d["fills"]]
        a.closed = d.get("closed", [])
        a.equity_history = d.get("equity_history", [])
        a.day_start_equity = D(d.get("day_start_equity", d["starting_cash"]))
        a.margin_call = False
        a.lot_method = d.get("lot_method", "fifo") if d.get("lot_method") in LOT_METHODS else "fifo"
        a.journal = d.get("journal", [])
        a.settings = lifecycle_settings.merged(d.get("settings"))
        a.life = AccountLife.from_plain(d.get("lifecycle"))
        return a
