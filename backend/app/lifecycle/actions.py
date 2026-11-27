"""Provider-agnostic corporate action records.

A feed announces actions through `MarketDataProvider.corporate_actions(start, end)`.
Two kinds exist:

- `dividend`: a cash dividend of `amount` per share. Holders of record at the start of
  `ex_date` (long positions receive, short positions pay) are paid on `pay_date`.
- `split`: `new` shares for every `old` share, effective at the start of `ex_date`.
  4-for-1 is new=4, old=1; a 1-for-10 reverse split is new=1, old=10; 3-for-2 is new=3, old=2.

`revision_key` (optional) names the event independently of the provider's record id, so a
record the provider later re-issues under a new id is still recognised as already applied.

A live feed whose data for a day is not in yet raises `ActionsPending`; the lifecycle then waits
and asks again on a later tick instead of skipping the day. A feed that cannot supply corporate
actions at all (for example the plan does not include them) raises `ActionsUnavailable`.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from ..core.money import ZERO, D

DIVIDEND, SPLIT = "dividend", "split"


class ActionsPending(Exception):
    """The feed has not got the corporate actions for the requested dates yet. `known` holds what it has."""

    def __init__(self, reason: str, known=()):
        super().__init__(reason)
        self.reason = reason
        self.known = list(known)


class ActionsUnavailable(Exception):
    """The feed cannot supply corporate actions (a lasting error such as a plan that does not include them)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CorporateAction:
    id: str
    symbol: str
    kind: str
    ex_date: date
    pay_date: Optional[date] = None
    amount: Decimal = ZERO  # cash per share, dividends only
    new: int = 1  # split ratio: `new` shares for every `old` share
    old: int = 1
    revision_key: str = ""

    def __post_init__(self):
        if self.kind == DIVIDEND:
            if self.amount <= 0 or self.pay_date is None or self.pay_date < self.ex_date:
                raise ValueError("a dividend needs a positive amount and a pay date on or after the ex-date")
        elif self.kind == SPLIT:
            if self.new <= 0 or self.old <= 0 or self.new == self.old:
                raise ValueError("a split needs two different positive whole numbers")
        else:
            raise ValueError(f"unknown corporate action kind {self.kind}")

    @property
    def is_reverse(self) -> bool:
        return self.kind == SPLIT and self.new < self.old

    @property
    def whole_forward_factor(self) -> Optional[int]:
        """N for an N-for-1 forward split (or any new/old that is a whole number), else None."""
        if self.kind == SPLIT and self.new > self.old and self.new % self.old == 0:
            return self.new // self.old
        return None

    def describe(self) -> str:
        if self.kind == DIVIDEND:
            return f"{self.amount.normalize():f} per share dividend of {self.symbol}"
        label = "reverse split" if self.is_reverse else "stock split"
        return f"{self.new}-for-{self.old} {label} of {self.symbol}"

    def to_plain(self) -> dict:
        out = {"id": self.id, "symbol": self.symbol, "kind": self.kind, "ex_date": self.ex_date.isoformat(),
               "pay_date": self.pay_date.isoformat() if self.pay_date else None, "amount": str(self.amount),
               "new": self.new, "old": self.old, "description": self.describe()}
        if self.revision_key:
            out["revision_key"] = self.revision_key
        return out

    def same_terms(self, other: "CorporateAction") -> bool:
        return (self.symbol, self.kind, self.ex_date, self.pay_date, self.amount, Decimal(self.new) / self.old) == \
            (other.symbol, other.kind, other.ex_date, other.pay_date, other.amount, Decimal(other.new) / other.old)

    @classmethod
    def from_plain(cls, d: dict) -> "CorporateAction":
        return cls(id=d["id"], symbol=d["symbol"], kind=d["kind"], ex_date=date.fromisoformat(d["ex_date"]),
                   pay_date=date.fromisoformat(d["pay_date"]) if d.get("pay_date") else None,
                   amount=D(d.get("amount", "0")), new=int(d.get("new", 1)), old=int(d.get("old", 1)),
                   revision_key=d.get("revision_key", ""))


def dividend(symbol: str, ex_date: date, pay_date: date, amount) -> CorporateAction:
    return CorporateAction(f"{symbol}-div-{ex_date.isoformat()}", symbol, DIVIDEND, ex_date, pay_date, D(amount))


def split(symbol: str, ex_date: date, new: int, old: int = 1) -> CorporateAction:
    return CorporateAction(f"{symbol}-split-{ex_date.isoformat()}", symbol, SPLIT, ex_date, new=int(new), old=int(old))
