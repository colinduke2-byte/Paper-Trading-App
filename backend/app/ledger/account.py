"""A paper account: ledger, FIFO tax lots, fills and closed-trade history."""
from dataclasses import dataclass
from decimal import Decimal

from ..core.models import BUY, Fill, from_plain, to_plain
from ..core.money import ZERO, D, cents
from ..options.symbology import describe
from .ledger import CAPITAL, CASH, FEES, REALIZED, Ledger, position_account


@dataclass
class Lot:
    qty: Decimal  # signed: negative for short
    price: Decimal
    ts: float
    cost: Decimal = ZERO  # remaining cost basis in cents (always positive)


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

    # ---- mutations -----------------------------------------------------
    def apply_fill(self, fill: Fill, multiplier: int) -> Decimal:
        """Book a fill. Returns the realised profit or loss of any closed quantity."""
        mult = Decimal(multiplier)
        sign = 1 if fill.side == BUY else -1
        lots = self.lots.setdefault(fill.symbol, [])
        remaining, closed_cost, closed_notional, pnl = fill.qty, ZERO, ZERO, ZERO
        while remaining > 0 and lots and (lots[0].qty > 0) != (sign > 0):
            lot = lots[0]
            take = min(remaining, abs(lot.qty))
            cost = lot.cost if take == abs(lot.qty) else cents(lot.cost * take / abs(lot.qty))
            lot.cost -= cost
            proceeds = cents(take * fill.price * mult)
            lot_pnl = (proceeds - cost) if lot.qty > 0 else (cost - proceeds)
            closed_cost += cost
            closed_notional += proceeds
            pnl += lot_pnl
            self.closed.append({"symbol": fill.symbol, "qty": str(take if lot.qty > 0 else -take),
                                "open_price": str(lot.price), "close_price": str(fill.price),
                                "pnl": str(lot_pnl), "opened_at": lot.ts, "closed_at": fill.ts})
            lot.qty += take if lot.qty < 0 else -take
            remaining -= take
            if lot.qty == 0:
                lots.pop(0)
        # Closed proceeds are the sum of the per-lot amounts, so profit and cash always agree.
        open_notional = cents(remaining * fill.price * mult)
        if remaining > 0:
            lots.append(Lot(sign * remaining, fill.price, fill.ts, open_notional))
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
    def to_plain(self) -> dict:
        return {"id": self.id, "name": self.name, "type": self.type, "starting_cash": str(self.starting_cash),
                "created_at": self.created_at, "ledger": self.ledger.to_plain(),
                "lots": {s: [[str(l.qty), str(l.price), l.ts, str(l.cost)] for l in ls] for s, ls in self.lots.items()},
                "fills": [to_plain(f) for f in self.fills], "closed": self.closed,
                "equity_history": self.equity_history, "day_start_equity": str(self.day_start_equity)}

    @classmethod
    def from_plain(cls, d: dict) -> "Account":
        a = cls.__new__(cls)
        a.id, a.name, a.type = d["id"], d["name"], d["type"]
        a.starting_cash = D(d["starting_cash"])
        a.created_at = d["created_at"]
        a.ledger = Ledger.from_plain(d["ledger"])
        a.lots = {s: [Lot(D(q), D(p), t, D(c)) for q, p, t, c in ls] for s, ls in d["lots"].items()}
        a.fills = [from_plain(Fill, f) for f in d["fills"]]
        a.closed = d.get("closed", [])
        a.equity_history = d.get("equity_history", [])
        a.day_start_equity = D(d.get("day_start_equity", d["starting_cash"]))
        a.margin_call = False
        return a
