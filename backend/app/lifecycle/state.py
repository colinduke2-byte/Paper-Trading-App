"""Per-account lifecycle state (settlement, day trades, accruals, dividends, margin calls).

Stored under the "lifecycle" key of `Account.to_plain()`. Every field has a default so
state files written before this existed still load.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from ..core.money import ZERO, D


@dataclass
class AccountLife:
    # [settle_date ISO, signed amount, fill id]: sale proceeds not yet settled
    unsettled: list = field(default_factory=list)
    # {"action", "symbol", "qty", "rate", "amount", "pay_date"}: dividends owed or due, by ex-date record
    pending_dividends: list = field(default_factory=list)
    # [trade date ISO, symbol, order id]: one entry per day trade
    day_trades: list = field(default_factory=list)
    # quantity opened today per symbol, for day-trade detection
    opened_date: Optional[str] = None
    opened: dict = field(default_factory=dict)
    accrued_interest: Decimal = ZERO
    accrued_borrow: Decimal = ZERO
    margin_call_since: Optional[float] = None
    margin_call_deficit: Decimal = ZERO

    def unsettled_total(self) -> Decimal:
        return sum((D(a) for _, a, _ in self.unsettled), ZERO)

    def to_plain(self) -> dict:
        return {"unsettled": [[d, str(a), r] for d, a, r in self.unsettled],
                "pending_dividends": self.pending_dividends,
                "day_trades": self.day_trades, "opened_date": self.opened_date,
                "opened": {s: str(q) for s, q in self.opened.items()},
                "accrued_interest": str(self.accrued_interest), "accrued_borrow": str(self.accrued_borrow),
                "margin_call_since": self.margin_call_since, "margin_call_deficit": str(self.margin_call_deficit)}

    @classmethod
    def from_plain(cls, d) -> "AccountLife":
        if not isinstance(d, dict):
            return cls()
        return cls(unsettled=[[x, D(a), r] for x, a, r in d.get("unsettled", [])],
                   pending_dividends=list(d.get("pending_dividends", [])),
                   day_trades=[list(x) for x in d.get("day_trades", [])],
                   opened_date=d.get("opened_date"), opened={s: D(q) for s, q in d.get("opened", {}).items()},
                   accrued_interest=D(d.get("accrued_interest", "0")), accrued_borrow=D(d.get("accrued_borrow", "0")),
                   margin_call_since=d.get("margin_call_since"),
                   margin_call_deficit=D(d.get("margin_call_deficit", "0")))
