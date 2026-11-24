"""Pure helpers for multi-leg orders: net pricing from quotes and the payoff at expiry.

A leg is {"symbol", "side", "ratio"}. For an option leg the ratio is contracts per unit;
for the (single, optional) stock leg it is shares per unit. Net prices are quoted the way
option prices are, per share: a unit of the order costs net x 100. Positive is a net debit
(you pay), negative a net credit (you receive).
"""
from decimal import Decimal
from typing import Optional

from ..core.models import BUY
from ..core.money import ZERO, cents
from ..options.symbology import multiplier, parse

H = Decimal(100)


def net_price(legs: list, quotes: dict) -> Optional[tuple]:
    """(net price per unit, [price of each leg]) buying at the ask and selling at the bid.

    None when any leg has no usable quote: a multi-leg order never trades unless every
    leg can trade at the same moment.
    """
    net, prices = ZERO, []
    for leg in legs:
        q = quotes.get(leg["symbol"])
        if q is None or q.ask <= 0 or q.bid < 0:
            return None
        px = q.ask if leg["side"] == BUY else q.bid
        sign = 1 if leg["side"] == BUY else -1
        net += sign * leg["ratio"] * multiplier(leg["symbol"]) * px
        prices.append(px)
    return net / H, prices


def _unit_value(legs: list, price: Decimal) -> Decimal:
    """Value in money of one unit at expiry with the underlying at `price`."""
    total = ZERO
    for leg in legs:
        c = parse(leg["symbol"])
        sign = 1 if leg["side"] == BUY else -1
        if c is None:
            total += sign * leg["ratio"] * price
        else:
            intrinsic = max(price - c.strike, ZERO) if c.kind == "C" else max(c.strike - price, ZERO)
            total += sign * leg["ratio"] * H * intrinsic
    return total


def payoff(legs: list, net: Decimal) -> dict:
    """Maximum profit, maximum loss and break-even prices of one unit entered at `net`.

    Exact when every option leg has the same expiry. With mixed expiries the payoff
    depends on what the later options are worth when the first one expires, so profit and
    break-evens are not given; the loss is still bounded by intrinsic value when every
    long option expires on or after every short one.
    """
    contracts = [(parse(l["symbol"]), l) for l in legs]
    options = [(c, l) for c, l in contracts if c]
    cost = net * H
    points = sorted({ZERO} | {c.strike for c, _ in options})
    values = [_unit_value(legs, p) - cost for p in points]
    # Slope of the payoff above the highest strike: net calls plus stock.
    slope = sum(((1 if l["side"] == BUY else -1) * l["ratio"] * (H if c else 1)
                 for c, l in contracts if c is None or c.kind == "C"), ZERO)
    expiries = {c.expiry for c, _ in options}
    out = {"at_expiry": len(expiries) == 1, "max_profit": None, "max_profit_unlimited": False,
           "max_loss": None, "max_loss_unlimited": slope < 0, "break_evens": []}
    if len(expiries) > 1:
        longs = [c.expiry for c, l in options if l["side"] == BUY]
        shorts = [c.expiry for c, l in options if l["side"] != BUY]
        if slope >= 0 and (not shorts or (longs and min(longs) >= max(shorts))):
            out["max_loss"] = cents(max(-min(values), ZERO))
        return out
    out["max_profit_unlimited"] = slope > 0
    if slope <= 0:
        out["max_profit"] = cents(max(max(values), ZERO))
    if slope >= 0:
        out["max_loss"] = cents(max(-min(values), ZERO))
    evens = []
    for (a, fa), (b, fb) in zip(zip(points, values), zip(points[1:], values[1:])):
        if fa == 0:
            evens.append(a)
        elif (fa < 0) != (fb < 0) and fb != 0:
            evens.append(a + (b - a) * (-fa) / (fb - fa))
    last, f_last = points[-1], values[-1]
    if f_last == 0:
        evens.append(last)
    elif slope != 0 and (f_last < 0) != (slope < 0):
        evens.append(last + (-f_last) / slope)
    out["break_evens"] = sorted({cents(e) for e in evens})
    return out
