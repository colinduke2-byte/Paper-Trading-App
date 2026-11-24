"""The soak feed: the synthetic feed plus a seeded schedule of extra corporate actions.

The synthetic feed announces quarterly dividends and one 2-for-1 split a year. A soak run also
needs the awkward cases, so this feed adds, from its own seeded generator, reverse splits,
odd-ratio splits (3-for-2, 5-for-4), larger forward splits and special dividends on random
symbols. The schedule depends only on the seed and the start time, so a reload (a fresh feed
built with the same seed) announces exactly the same actions.

`shock` moves one symbol's price at once, like a feed reconnecting after a gap. It is how the
soak gets large moves (margin calls, liquidations, options finishing in the money): the
synthetic random walk caps each step at five seconds of volatility, so even a simulated year
of large clock jumps barely moves prices on its own.
"""
import random
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from app.core.money import CENT, cents
from app.lifecycle.actions import dividend as dividend_action, split as split_action
from app.marketdata.synthetic import UNIVERSE, SyntheticFeed

SPLIT_RATIOS = ((1, 4), (1, 10), (3, 2), (5, 4), (3, 1), (2, 3))  # (new, old)


def extra_actions(seed: int, start: float, years: int = 6) -> list:
    rng = random.Random(f"soak-actions-{seed}")
    first = date(1970, 1, 1) + timedelta(days=int(start // 86400))
    out = []
    for _ in range(3 * years):
        day = first + timedelta(days=rng.randrange(1, 365 * years))
        while day.weekday() >= 5:
            day += timedelta(days=1)
        sym, _, _, price, *_ = UNIVERSE[rng.randrange(len(UNIVERSE))]
        if rng.random() < 0.6:
            new, old = SPLIT_RATIOS[rng.randrange(len(SPLIT_RATIOS))]
            out.append(split_action(sym, day, new, old))
        else:
            amount = cents(Decimal(repr(price)) * Decimal(rng.choice(("0.005", "0.01", "0.03"))))
            out.append(dividend_action(sym, day, day + timedelta(days=rng.randrange(0, 21)), max(amount, CENT)))
    return out


class SoakFeed(SyntheticFeed):
    def __init__(self, seed: int, now: float, always_open: bool = True, history_days: int = 60,
                 extras: bool = True, start: float | None = None):
        super().__init__(seed=seed, now=now, history_days=history_days, always_open=always_open)
        self._extra = extra_actions(seed, now if start is None else start) if extras else []

    def corporate_actions(self, start, end) -> list:
        out = super().corporate_actions(start, end)
        ids = {a.id for a in out}
        for a in self._extra:
            if start <= a.ex_date <= end and a.id not in ids:
                out.append(a)
                ids.add(a.id)
        return out


def shock(feed, symbol: str, factor: Decimal) -> None:
    """Move one symbol's quote to last x factor at once (bid and ask around it, a cent apart at least)."""
    q = feed.quotes.get(symbol)
    if q is None:
        return
    last = max(cents(q.last * factor), CENT)
    half = max(CENT, cents(last * Decimal("0.0002")))
    bid = max(last - half, CENT)
    ask = max(last + half, bid + CENT)
    feed.quotes[symbol] = replace(q, last=last, bid=bid, ask=ask, high=max(q.high or last, last),
                                  low=min(q.low or last, last))
