"""Margin and buying-power rules.

Margin account (Regulation T style):
- Stock: 50% initial, 25% maintenance long, 30% maintenance short.
- Long options: paid in full (no loan value).
- Short options: covered calls need no margin; otherwise the standard uncovered
  formula: premium + max(20% of underlying - out-of-the-money amount, 10% floor).
Cash account: no short stock, no uncovered calls, short puts fully cash-secured.

Positions are {symbol: signed quantity}. `marks` gives a price per symbol and must
include the underlying of every option held.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from ..core.money import ZERO, cents
from ..options.symbology import parse

INITIAL = Decimal("0.5")
MAINT_LONG = Decimal("0.25")
MAINT_SHORT = Decimal("0.30")
H = Decimal(100)


@dataclass
class Evaluation:
    ok: bool
    reason: Optional[str]
    equity: Decimal
    requirement: Decimal

    @property
    def excess(self) -> Decimal:
        return self.equity - self.requirement


def equity(cash: Decimal, positions: dict, marks: dict) -> Decimal:
    total = cash
    for sym, qty in positions.items():
        total += qty * marks[sym] * (H if parse(sym) else 1)
    return cents(total)


def _short_option_requirement(positions: dict, marks: dict) -> Decimal:
    """Requirement for short options after covering calls with long stock."""
    req = ZERO
    shares_free = {s: q for s, q in positions.items() if q > 0 and not parse(s)}
    shorts = sorted((s for s, q in positions.items() if q < 0 and parse(s)), key=lambda s: parse(s).strike)
    for sym in shorts:
        c, contracts = parse(sym), -positions[sym]
        u, premium = marks[c.underlying], marks[sym]
        if c.kind == "C":
            covered = min(contracts, shares_free.get(c.underlying, ZERO) // H)
            shares_free[c.underlying] = shares_free.get(c.underlying, ZERO) - covered * H
            naked = contracts - covered
            otm = max(c.strike - u, ZERO)
            per = premium + max(Decimal("0.20") * u - otm, Decimal("0.10") * u)
        else:
            naked = contracts
            otm = max(u - c.strike, ZERO)
            per = premium + max(Decimal("0.20") * u - otm, Decimal("0.10") * c.strike)
        req += naked * per * H
    return req


def requirement(positions: dict, marks: dict, maintenance: bool = False) -> Decimal:
    req = ZERO
    for sym, qty in positions.items():
        if parse(sym):
            if qty > 0:
                req += qty * marks[sym] * H
            continue
        mv = abs(qty) * marks[sym]
        if maintenance:
            req += mv * (MAINT_LONG if qty > 0 else MAINT_SHORT)
        else:
            req += mv * INITIAL
    return cents(req + _short_option_requirement(positions, marks))


def evaluate(account_type: str, cash: Decimal, positions: dict, marks: dict) -> Evaluation:
    """Would this state be acceptable when opening new risk?"""
    eq = equity(cash, positions, marks)
    if account_type == "margin":
        req = requirement(positions, marks)
        ok = eq >= req
        return Evaluation(ok, None if ok else "insufficient buying power", eq, req)
    # Cash account
    secured = ZERO
    shares_free = {s: q for s, q in positions.items() if not parse(s)}
    for sym, qty in positions.items():
        c = parse(sym)
        if c is None:
            if qty < 0:
                return Evaluation(False, "short selling needs a margin account", eq, ZERO)
        elif qty < 0:
            if c.kind == "P":
                secured += -qty * c.strike * H
            else:
                shares_free[c.underlying] = shares_free.get(c.underlying, ZERO) + qty * H
                if shares_free[c.underlying] < 0:
                    return Evaluation(False, "uncovered calls need a margin account", eq, ZERO)
    secured = cents(secured)
    if cash < secured:
        reason = "insufficient cash" if secured == 0 else "insufficient cash to secure short puts"
        return Evaluation(False, reason, eq, secured)
    return Evaluation(True, None, eq, secured)


def buying_power(account_type: str, cash: Decimal, positions: dict, marks: dict) -> dict:
    ev = evaluate(account_type, cash, positions, marks)
    if account_type == "margin":
        excess = max(ev.excess, ZERO)
        return {"stock": cents(excess / INITIAL), "option": excess}
    avail = max(cash - ev.requirement, ZERO)
    return {"stock": avail, "option": avail}
