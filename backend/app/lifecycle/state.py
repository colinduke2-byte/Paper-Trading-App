"""Per-account lifecycle state (settlement, day trades, accruals, dividends, margin calls).

Stored under the "lifecycle" key of `Account.to_plain()`. Every field has a default so
state files written before this existed still load.
"""
from dataclasses import dataclass, field, fields
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
    # lots opened today per symbol, for day-trade detection: [opening order id, open qty, counted]
    opened_date: Optional[str] = None
    opened: dict = field(default_factory=dict)
    accrued_interest: Decimal = ZERO
    accrued_borrow: Decimal = ZERO
    # what the unposted accruals were charged at: {"interest"|"borrow": {rate: [amount, days]}}
    accrual_rates: dict = field(default_factory=dict)
    margin_call_since: Optional[float] = None
    margin_call_deficit: Decimal = ZERO
    margin_call_ok_since: Optional[float] = None  # equity back at or above maintenance since
    # grace period used so far: seconds counted while the market was open and the simulator running,
    # and the tick that was last counted from
    margin_call_counted: float = 0.0
    margin_call_last: Optional[float] = None
    liquidation_paused: bool = False
    prices_missing: list = field(default_factory=list)
    log: list = field(default_factory=list)  # [ts, kind, message]: margin calls, liquidation, adjustments

    def copy(self) -> "AccountLife":
        """An independent copy, for rolling a failed request back. Every field holds only lists, dicts and
        immutable values (str, Decimal, numbers, None), so copying the containers is enough; much faster
        than copy.deepcopy for an account with thousands of unsettled sales."""
        return AccountLife(**{f.name: _copy(getattr(self, f.name)) for f in fields(self)})

    def unsettled_total(self) -> Decimal:
        return sum((D(a) for _, a, _ in self.unsettled), ZERO)

    def to_plain(self) -> dict:
        return {"unsettled": [[d, str(a), r] for d, a, r in self.unsettled],
                "pending_dividends": self.pending_dividends,
                "day_trades": self.day_trades, "opened_date": self.opened_date,
                "opened": {s: [[i, str(q), c] for i, q, c in lots] for s, lots in self.opened.items()},
                "accrued_interest": str(self.accrued_interest), "accrued_borrow": str(self.accrued_borrow),
                "accrual_rates": self.accrual_rates,
                "margin_call_since": self.margin_call_since, "margin_call_deficit": str(self.margin_call_deficit),
                "margin_call_ok_since": self.margin_call_ok_since, "liquidation_paused": self.liquidation_paused,
                "margin_call_counted": self.margin_call_counted, "margin_call_last": self.margin_call_last,
                "prices_missing": self.prices_missing, "log": self.log}

    @classmethod
    def from_plain(cls, d) -> "AccountLife":
        if not isinstance(d, dict):
            return cls()
        return cls(unsettled=[[x, D(a), r] for x, a, r in d.get("unsettled", [])],
                   pending_dividends=list(d.get("pending_dividends", [])),
                   day_trades=[list(x) for x in d.get("day_trades", [])],
                   opened_date=d.get("opened_date"), opened={s: _lots(v) for s, v in d.get("opened", {}).items()},
                   accrued_interest=D(d.get("accrued_interest", "0")), accrued_borrow=D(d.get("accrued_borrow", "0")),
                   accrual_rates=dict(d.get("accrual_rates") or {}),
                   margin_call_since=d.get("margin_call_since"),
                   margin_call_deficit=D(d.get("margin_call_deficit", "0")),
                   margin_call_ok_since=d.get("margin_call_ok_since"),
                   margin_call_counted=float(d.get("margin_call_counted") or 0.0),
                   margin_call_last=d.get("margin_call_last"),
                   liquidation_paused=bool(d.get("liquidation_paused", False)),
                   prices_missing=list(d.get("prices_missing", [])), log=[list(x) for x in d.get("log", [])])


def _lots(v) -> list:
    if isinstance(v, (str, int)):  # format before opening lots were tracked: one uncounted lot
        return [[None, D(v), False]]
    return [[i, D(q), bool(c)] for i, q, c in v]


def _copy(v):
    if isinstance(v, list):
        return [_copy(x) for x in v]
    if isinstance(v, dict):
        return {k: _copy(x) for k, x in v.items()}
    return v
