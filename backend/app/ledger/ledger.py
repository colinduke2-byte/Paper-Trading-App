"""Append-only double-entry ledger. Every transaction's postings sum to zero.

Sign convention: assets (cash, positions at cost) are positive; income and
contributed capital are negative; expenses are positive.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from ..core.money import ZERO, D

CASH = "cash"
CAPITAL = "equity:capital"
REALIZED = "income:realized"
FEES = "expense:fees"


def position_account(symbol: str) -> str:
    return f"pos:{symbol}"


@dataclass
class Transaction:
    id: int
    ts: float
    kind: str
    memo: str
    postings: list  # [(account, Decimal)]
    ref: str = ""


class Ledger:
    def __init__(self):
        self.transactions: list[Transaction] = []
        self.balances: dict[str, Decimal] = {}

    def post(self, ts: float, kind: str, memo: str, postings, ref: str = "") -> Transaction:
        postings = [(a, D(v)) for a, v in postings if D(v) != 0]
        total = sum((v for _, v in postings), ZERO)
        if total != 0:
            raise ValueError(f"unbalanced transaction ({total}): {postings}")
        txn = Transaction(len(self.transactions) + 1, ts, kind, memo, postings, ref)
        self.transactions.append(txn)
        for a, v in postings:
            self.balances[a] = self.balances.get(a, ZERO) + v
        return txn

    def balance(self, account: str) -> Decimal:
        return self.balances.get(account, ZERO)

    def recompute(self) -> dict:
        out: dict[str, Decimal] = {}
        for t in self.transactions:
            for a, v in t.postings:
                out[a] = out.get(a, ZERO) + v
        return out

    def to_plain(self) -> list:
        return [{"id": t.id, "ts": t.ts, "kind": t.kind, "memo": t.memo, "ref": t.ref,
                 "postings": [[a, str(v)] for a, v in t.postings]} for t in self.transactions]

    @classmethod
    def from_plain(cls, rows: list) -> "Ledger":
        led = cls()
        for r in rows:
            led.post(r["ts"], r["kind"], r["memo"], [(a, D(v)) for a, v in r["postings"]], r.get("ref", ""))
        return led
