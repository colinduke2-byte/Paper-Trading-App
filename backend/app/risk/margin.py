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
  short option as uncovered, so pairing can never raise a requirement. When longs can
  cover shorts in more than one way, every way of pairing them is tried (for all but very
  large books) and the cheapest is used; each one is a valid cover, so a covered group
  always holds its worst loss.
Cash account: no short stock. A long option is NOT cover in a cash account, because
assignment settles in shares and the account must be able to take or deliver them: a
short call needs the shares, a short put needs strike x 100 in cash. Spreads whose short
leg is not secured that way need a margin account.

Positions are {symbol: signed quantity}. `marks` gives a price per symbol and must
include the underlying of every option held.
"""
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from math import gcd
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


def _after_stock(shares: Decimal, opts: dict) -> dict:
    """{symbol: short contracts} left once long stock has covered calls (lowest strikes first)."""
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
    return after_stock


def _preference(long_sym: str, short) -> tuple:
    lc = parse(long_sym)
    gap = (short.strike - lc.strike) if short.kind == "C" else (lc.strike - short.strike)
    # First a long that is at least as good as the short (closest first), then the narrowest loss.
    return (0, gap, lc.expiry, long_sym) if gap >= 0 else (1, -gap, lc.expiry, long_sym)


def _pair(shorts: dict, longs: dict, short_order, long_order=None) -> dict:
    """Greedy pairing {(short, long): contracts}: shorts in the given order take longs in the given
    order (or by preference), only ever a long of the same kind that expires on or after the short."""
    left = dict(longs)
    pairs = {}
    for sym in short_order:
        c, n = parse(sym), shorts[sym]
        usable = [l for l in left if parse(l).kind == c.kind and parse(l).expiry >= c.expiry]
        for l in (sorted(usable, key=lambda l: _preference(l, c)) if long_order is None
                  else [l for l in long_order if l in usable]):
            take = min(n, left[l])
            if take > 0:
                pairs[(sym, l)] = take
                left[l] -= take
                n -= take
    return pairs


_MAX_EXACT = 256  # books with at most this many possible pairings are searched completely


def _danger(sym: str) -> tuple:
    c = parse(sym)
    return (c.strike if c.kind == "C" else -c.strike, sym)  # lowest-strike calls, highest-strike puts first


def _cells(shorts: dict, longs: dict) -> list:
    """(short, long) pairs where the long may cover the short: same kind, expiring on or after it."""
    return [(s, l) for s in sorted(shorts) for l in sorted(longs)
            if parse(s).kind == parse(l).kind and parse(l).expiry >= parse(s).expiry]


def _pairing_count(shorts: dict, longs: dict, step: int = 1) -> int:
    """Upper bound on the number of distinct pairings when contracts move in blocks of `step`."""
    count = 1
    for s, l in _cells(shorts, longs):
        count *= int(min(shorts[s], longs[l])) // step + 1
    return count


@lru_cache(maxsize=2048)
def _pairings(shorts_items: tuple, longs_items: tuple) -> tuple:
    """Ways of pairing uncovered shorts with long options, as tuples of ((short, long), contracts).

    Small books (nearly all real ones) are searched completely, contract by contract: every
    split of every short between the longs that may cover it, including leaving it
    uncovered. The search must be per contract: options that share an expiry are valued
    together, so splitting a short half-and-half between two longs can be cheaper than
    either whole pairing (short 2 puts between 1 lower and 1 higher long put), and a search
    in whole lots would miss it. Books too large for that are searched in the smallest
    block size (a divisor of the common lot size) that keeps the search small, together
    with a handful of greedy orders; that is still always a valid cover, just not
    guaranteed to be the cheapest. The result depends on the positions only, so it is cached.
    """
    shorts, longs = dict(shorts_items), dict(longs_items)
    found = {}
    lot = gcd(*(int(n) for n in list(shorts.values()) + list(longs.values())))
    step = next((d for d in range(1, lot + 1) if lot % d == 0 and _pairing_count(shorts, longs, d) <= _MAX_EXACT), None)
    if step is not None:
        cells = _cells(shorts, longs)
        left_s = {s: int(n) // step for s, n in shorts.items()}
        left_l = {l: int(n) // step for l, n in longs.items()}

        def walk(i: int, acc: list) -> None:
            if i == len(cells):
                found.setdefault(tuple(acc), None)
                return
            sym, l = cells[i]
            for n in range(min(left_s[sym], left_l[l]) + 1):
                left_s[sym] -= n
                left_l[l] -= n
                walk(i + 1, acc + [((sym, l), Decimal(n * step))] if n else acc)
                left_s[sym] += n
                left_l[l] += n

        walk(0, [])
        if step == 1:
            return tuple(found)
    by_danger = sorted(shorts, key=lambda s: (parse(s).kind,) + _danger(s))
    by_expiry = sorted(by_danger, key=lambda s: parse(s).expiry)
    long_orders = [None, sorted(longs), sorted(longs, reverse=True), sorted(longs, key=lambda l: parse(l).expiry),
                   sorted(longs, key=lambda l: parse(l).expiry, reverse=True)]
    for short_order in (by_danger, by_danger[::-1], by_expiry, by_expiry[::-1]):
        for long_order in long_orders:
            found.setdefault(tuple(sorted(_pair(shorts, longs, short_order, long_order).items())), None)
    found.setdefault((), None)
    return tuple(found)


def _split(after_stock: dict, opts: dict, pairs) -> tuple:
    """(groups, naked, free) for one pairing: see _cover."""
    free = {s: q for s, q in opts.items() if q > 0}
    naked = dict(after_stock)
    groups: dict = {}
    for (short, long_), n in pairs:
        g = groups.setdefault(parse(short).expiry, {})
        g[short] = g.get(short, ZERO) - n
        g[long_] = g.get(long_, ZERO) + n
        naked[short] -= n
        free[long_] -= n
    return groups, {s: n for s, n in naked.items() if n > 0}, {s: q for s, q in free.items() if q > 0}


def _covers(shares: Decimal, opts: dict) -> tuple:
    """(after_stock, [every (groups, naked, free) worth pricing]) for the options of one underlying."""
    after_stock = _after_stock(shares, opts)
    longs = {s: q for s, q in opts.items() if q > 0}
    if not after_stock or not longs:
        return after_stock, [({}, after_stock, longs)]
    pairings = _pairings(tuple(sorted(after_stock.items())), tuple(sorted(longs.items())))
    return after_stock, [_split(after_stock, opts, pairs) for pairs in pairings]


def _cover(shares: Decimal, opts: dict) -> tuple:
    """Decide what covers each short option of one underlying (the first, greedy, pairing).

    Returns (after_stock, groups, naked, free):
    - after_stock: {symbol: short contracts left once long stock has covered calls}
    - groups: {expiry of the shorts: {symbol: signed contracts}}, each short with its long cover
    - naked: {symbol: short contracts with no cover at all}
    - free: {symbol: long contracts not used as cover}
    """
    after_stock = _after_stock(shares, opts)
    longs = {s: q for s, q in opts.items() if q > 0}
    order = sorted(after_stock, key=lambda s: (parse(s).kind,) + _danger(s))
    return (after_stock,) + _split(after_stock, opts, _pair(after_stock, longs, order).items())


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
    after_stock, candidates = _covers(shares, opts)
    longs = sum((q * marks[s] for s, q in opts.items() if q > 0), ZERO) * H
    best = longs + _uncovered_requirement(after_stock, marks)  # every short treated as uncovered
    for groups, naked, free in candidates:
        if groups:
            best = min(best, sum((q * marks[s] for s, q in free.items()), ZERO) * H
                       + sum((_group_requirement(g, marks) for g in groups.values()), ZERO)
                       + _uncovered_requirement(naked, marks))
    return best


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
        # Assignment settles in shares, so a long option covers nothing here: a short call
        # needs the shares to deliver and a short put needs the cash to pay for them.
        for sym, n in _after_stock(positions.get(underlying, ZERO), opts).items():
            c = parse(sym)
            if c.kind == "C":
                return Evaluation(False, "uncovered calls need a margin account", eq, ZERO, structural=True)
            secured += n * c.strike * H
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
