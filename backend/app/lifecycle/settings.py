"""Per-account lifecycle settings: a small JSON object stored on the account.

Numbers are stored as strings so they stay exact Decimals, with at most 4 decimal
places. Every rule can be turned off except cash dividends, which are always paid
(the price always drops by the dividend on the ex-date). Defaults are typical of a
US retail broker.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ..core.money import parse

# key: (kind, default, label, help text, (min, max) for numbers)
SPEC = {
    "dividend_reinvestment": ("bool", False, "Reinvest dividends",
                              "Buy more shares with each dividend received (fractional shares allowed).", None),
    "settlement": ("bool", True, "T+1 settlement",
                   "Proceeds from stock and option sales settle on the next business day.", None),
    "settled_cash_only": ("bool", True, "Buy only with settled cash (cash accounts)",
                          "Stops you buying with money from a sale that has not settled yet, as real cash accounts require "
                          "(breaking it is called a good-faith violation).", None),
    "pattern_day_trader": ("bool", True, "Pattern day trader rule (margin accounts)",
                           "A day trade is buying and selling the same stock or option on the same day. With this on, a margin "
                           "account worth less than the minimum below can make only 3 day trades in any 5 business days.", None),
    "pdt_min_equity": ("number", "25000", "Pattern day trader minimum account value",
                       "Account value needed to day trade without the 3-trade limit.", ("0", "100000000")),
    "margin_interest": ("bool", True, "Margin interest",
                        "Charge interest on borrowed cash (a negative cash balance) in margin accounts.", None),
    "margin_interest_rate": ("number", "8", "Margin interest rate (% a year)",
                             "Charged on money you borrow (a negative cash balance), worked out daily and taken from cash on "
                             "the 1st of each month.",
                             ("0", "50")),
    "borrow_fee": ("bool", True, "Short borrow fee",
                   "Charge a fee for borrowing shares sold short.", None),
    "borrow_fee_rate": ("number", "0.5", "Borrow fee rate (% a year)",
                        "Charged daily on the value of shares you have sold short, taken from cash once a month.", ("0", "200")),
    "margin_call_liquidation": ("bool", True, "Liquidate on unmet margin calls",
                                "Sell positions with market orders when a margin call is not met in time.", None),
    "margin_call_grace_hours": ("number", "48", "Margin call grace period (hours)",
                                "Time to meet a margin call before positions are liquidated.", ("0", "720")),
}


def defaults() -> dict:
    return {k: v[1] for k, v in SPEC.items()}


def merged(stored) -> dict:
    """Defaults overlaid with any valid stored values (unknown keys are dropped)."""
    out = defaults()
    if isinstance(stored, dict):
        for k, v in stored.items():
            if k in SPEC:
                try:
                    out[k] = _coerce(k, v)
                except ValueError:
                    pass
    return out


def _coerce(key: str, value):
    kind, default, label, _, bounds = SPEC[key]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{label} must be on or off.")
        return value
    try:
        d = parse(value)
    except (ValueError, InvalidOperation, ArithmeticError):
        raise ValueError(f"{label} must be a number, for example {default}.")
    lo, hi = Decimal(bounds[0]), Decimal(bounds[1])
    if not d.is_finite() or abs(d.adjusted()) > 20 and d != 0:
        raise ValueError(f"{label} must be a plain number from {lo} to {hi}.")
    if d < lo or d > hi:
        raise ValueError(f"{label} must be from {lo} to {hi}.")
    d = d.quantize(Decimal("0.0001"), ROUND_HALF_UP) + 0  # at most 4 decimals; "-0" becomes "0"
    if d < lo:
        raise ValueError(f"{label} must be from {lo} to {hi}.")
    return format(d.normalize(), "f")


def update(current: dict, changes: dict) -> dict:
    """Validate a partial update. Raises ValueError naming the bad setting."""
    if not isinstance(changes, dict):
        raise ValueError("Settings must be a JSON object.")
    out = dict(current)
    for k, v in changes.items():
        if k not in SPEC:
            raise ValueError(f"Unknown setting {k}.")
        out[k] = _coerce(k, v)
    return out


def number(settings: dict, key: str) -> Decimal:
    return Decimal(str(settings.get(key, SPEC[key][1])))


def describe() -> list:
    return [{"key": k, "type": v[0], "default": v[1], "label": v[2], "help": v[3],
             "min": v[4][0] if v[4] else None, "max": v[4][1] if v[4] else None} for k, v in SPEC.items()]
