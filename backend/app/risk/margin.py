"""Margin and buying-power rules.

Margin account (Regulation T style):
- Stock: 50% initial, 25% maintenance long, 30% maintenance short.
- Long options: paid in full (no loan value).
- Short options, in this order:
  1. Calls covered by long stock need no margin.
  2. Short options covered by long options of the same kind on the same underlying, where
     the long expires on or after the short, form a defined-risk group (vertical, calendar,
     diagonal, butterfly, iron condor...). All covered options that share the short's expiry
     are valued together at every strike, so only one wing of an iron condor is charged.
     The group's requirement is its current net market value plus its worst loss of
     intrinsic value. Because equity already counts the options at market, this holds
     exactly the maximum loss of the spread out of the account's own cash.
  3. Whatever is still uncovered uses the standard uncovered formula:
     premium + max(20% of underlying - out-of-the-money amount, 10% floor).
     A short call and a short put with the same expiry (straddle, strangle) cannot both
     lose, so a pair costs the greater of the two requirements plus the other premium.
  The covered treatment is used only when it is not more expensive than treating every
  short option as uncovered, so pairing can never raise a requirement.
Cash account: no short stock, no uncovered calls. Short options must be covered by stock,
by a long option as above with the worst loss held in cash, or (puts) fully cash-secured.

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
    structural: bool = False  # refused because of what is held, not because of how much cash there is

    @property
    def excess(self) -> Decimal:
        return self.equity - self.requirement


def equity(cash: Decimal, positions: dict, marks: dict) -> Decimal:
    total = cash
    for sym, qty in positions.items():
        total += qty * marks[sym] * (H if parse(sym) else 1)
    return cents(total)


def _books(positions: dict) -> dict:
    """{underlying: {option symbol: signed contracts}} for every underlying with options."""
    books: dict = {}
    for sym, qty in positions.items():
        c = parse(sym)
        if c is not None and qty != 0:
            books.setdefault(c.underlying, {})[sym] = qty
    return books


def _cover(shares: Decimal, opts: dict) -> tuple:
    """Decide what covers each short option of one underlying.

    Returns (after_stock, groups, naked, free):
    - after_stock: {symbol: short contracts left once long stock has covered calls}
    - groups: {expiry of the shorts: {symbol: signed contracts}}, each short with its long cover
    - naked: {symbol: short contracts with no cover at all}
    - free: {symbol: long contracts not used as cover}
    """
    shares_free = max(shares, ZERO)
    after_stock = {}
    for sym in sorted((s for s, q in opts.items() if q < 0), key=lambda s: parse(s).strike):
        c, n = parse(sym), -opts[sym]
        if c.kind == "C":
            covered = min(n, shares_free // H)
            shares_free -= covered * H
            n -= covered
        if n > 0:
            after_stock[sym] = n
    free = {s: q for s, q in opts.items() if q > 0}
    groups, naked = {}, {}

    def preference(long_sym: str, short) -> tuple:
        lc = parse(long_sym)
        gap = (short.strike - lc.strike) if short.kind == "C" else (lc.strike - short.strike)
        # First a long that is at least as good as the short (closest first), then the narrowest loss.
        return (0, gap, lc.expiry, long_sym) if gap >= 0 else (1, -gap, lc.expiry, long_sym)

    # Most dangerous shorts first: lowest-strike calls, highest-strike puts.
    for sym in sorted(after_stock, key=lambda s: (parse(s).kind, parse(s).strike if parse(s).kind == "C" else -parse(s).strike, s)):
        c, n = parse(sym), after_stock[sym]
        while n > 0:
            candidates = [l for l, q in free.items() if q > 0 and parse(l).kind == c.kind and parse(l).expiry >= c.expiry]
            if not candidates:
                break
            best = min(candidates, key=lambda l: preference(l, c))
            take = min(n, free[best])
            free[best] -= take
            n -= take
            g = groups.setdefault(c.expiry, {})
            g[sym] = g.get(sym, ZERO) - take
            g[best] = g.get(best, ZERO) + take
        if n > 0:
            naked[sym] = n
    return after_stock, groups, naked, {s: q for s, q in free.items() if q > 0}


def worst_value(group: dict) -> Decimal:
    """Lowest intrinsic value {option symbol: signed contracts} can have, over every underlying price.

    Only meaningful when short calls do not outnumber long calls (otherwise there is no floor).
    The value is piecewise linear in the price, so the minimum is at zero or at a strike.
    """
    contracts = [(parse(s), q) for s, q in group.items()]
    worst = None
    for price in {ZERO} | {c.strike for c, _ in contracts}:
        v = sum((q * (max(price - c.strike, ZERO) if c.kind == "C" else max(c.strike - price, ZERO))
                 for c, q in contracts), ZERO) * H
        worst = v if worst is None else min(worst, v)
    return worst if worst is not None else ZERO


def _group_requirement(group: dict, marks: dict) -> Decimal:
    net_value = sum((q * marks[s] for s, q in group.items()), ZERO) * H
    return max(net_value - worst_value(group), ZERO)


def _uncovered_per_contract(sym: str, marks: dict) -> Decimal:
    c = parse(sym)
    u, premium = marks[c.underlying], marks[sym]
    if c.kind == "C":
        otm = max(c.strike - u, ZERO)
        return (premium + max(Decimal("0.20") * u - otm, Decimal("0.10") * u)) * H
    otm = max(u - c.strike, ZERO)
    return (premium + max(Decimal("0.20") * u - otm, Decimal("0.10") * c.strike)) * H


def _uncovered_requirement(naked: dict, marks: dict) -> Decimal:
    """Uncovered formula for {symbol: short contracts}; same-expiry call/put pairs count once."""
    req = ZERO
    by_expiry: dict = {}
    for sym, n in naked.items():
        c = parse(sym)
        by_expiry.setdefault(c.expiry, {"C": [], "P": []})[c.kind].append(
            [_uncovered_per_contract(sym, marks), marks[sym] * H, n])
    for sides in by_expiry.values():
        calls = sorted(sides["C"], key=lambda r: -r[0])
        puts = sorted(sides["P"], key=lambda r: -r[0])
        i = j = 0
        while i < len(calls) and j < len(puts):
            (rc, pc, nc), (rp, pp, np_) = calls[i], puts[j]
            n = min(nc, np_)
            req += n * (rc + pp if rc >= rp else rp + pc)
            calls[i][2] -= n
            puts[j][2] -= n
            i += calls[i][2] == 0
            j += puts[j][2] == 0
        req += sum((r[0] * r[2] for r in calls[i:] + puts[j:]), ZERO)
    return req


def _option_requirement(shares: Decimal, opts: dict, marks: dict) -> Decimal:
    """Requirement for all options of one underlying (long options included)."""
    after_stock, groups, naked, free = _cover(shares, opts)
    longs = sum((q * marks[s] for s, q in opts.items() if q > 0), ZERO) * H
    all_uncovered = longs + _uncovered_requirement(after_stock, marks)
    if not groups:
        return all_uncovered
    covered = (sum((q * marks[s] for s, q in free.items()), ZERO) * H
               + sum((_group_requirement(g, marks) for g in groups.values()), ZERO)
               + _uncovered_requirement(naked, marks))
    return min(covered, all_uncovered)


def requirement(positions: dict, marks: dict, maintenance: bool = False) -> Decimal:
    req = ZERO
    for sym, qty in positions.items():
        if parse(sym):
            continue
        mv = abs(qty) * marks[sym]
        if maintenance:
            req += mv * (MAINT_LONG if qty > 0 else MAINT_SHORT)
        else:
            req += mv * INITIAL
    for underlying, opts in _books(positions).items():
        req += _option_requirement(positions.get(underlying, ZERO), opts, marks)
    return cents(req)


def evaluate(account_type: str, cash: Decimal, positions: dict, marks: dict) -> Evaluation:
    """Would this state be acceptable when opening new risk?"""
    eq = equity(cash, positions, marks)
    if account_type == "margin":
        req = requirement(positions, marks)
        ok = eq >= req
        return Evaluation(ok, None if ok else "insufficient buying power", eq, req)
    # Cash account
    if any(qty < 0 and not parse(sym) for sym, qty in positions.items()):
        return Evaluation(False, "short selling needs a margin account", eq, ZERO, structural=True)
    secured = ZERO
    for underlying, opts in _books(positions).items():
        _, groups, naked, _ = _cover(positions.get(underlying, ZERO), opts)
        for sym, n in naked.items():
            c = parse(sym)
            if c.kind == "C":
                return Evaluation(False, "uncovered calls need a margin account", eq, ZERO, structural=True)
            secured += n * c.strike * H
        # A spread is secured by holding its worst possible loss in cash.
        secured += sum((max(-worst_value(g), ZERO) for g in groups.values()), ZERO)
    secured = cents(secured)
    if cash < secured:
        reason = "insufficient cash" if secured == 0 else "insufficient cash to secure short options"
        return Evaluation(False, reason, eq, secured)
    return Evaluation(True, None, eq, secured)


def buying_power(account_type: str, cash: Decimal, positions: dict, marks: dict) -> dict:
    ev = evaluate(account_type, cash, positions, marks)
    if account_type == "margin":
        excess = max(ev.excess, ZERO)
        return {"stock": cents(excess / INITIAL), "option": excess}
    avail = max(cash - ev.requirement, ZERO)
    return {"stock": avail, "option": avail}
