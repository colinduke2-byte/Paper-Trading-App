"""Exact decimal helpers. Money and quantities are never floats."""
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP

ZERO = Decimal("0")
CENT = Decimal("0.01")
TENTH_MILL = Decimal("0.0001")


def D(x) -> Decimal:
    """Convert to Decimal. Floats are rejected on purpose."""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, bool) or isinstance(x, float):
        raise TypeError(f"refusing to build money from {type(x).__name__}: {x!r}")
    if isinstance(x, (int, str)):
        return Decimal(x)
    raise TypeError(f"cannot convert {type(x).__name__} to Decimal")


# Bounds for every number that arrives from outside (API bodies, query strings). parse() refuses anything
# outside them, so no later arithmetic, formatting or storage ever sees an extreme exponent. Each field then
# applies its own, tighter domain (prices, quantities, percentages, cash). See docs/LIMITS.md.
MAX_NUMBER_TEXT = 40                 # characters in a number written as text
MAX_DECIMAL_PLACES = 8
MAX_MAGNITUDE = Decimal("1e12")      # largest absolute value accepted anywhere
_QUANTUM = Decimal(1).scaleb(-MAX_DECIMAL_PLACES)


def _label(what: str) -> str:
    return what[:1].upper() + what[1:]


def parse(x, what: str = "number") -> Decimal:
    """Parse untrusted input into a bounded Decimal, or raise ValueError naming the field.

    Accepts JSON numbers (ints and floats, read via their shortest text form), Decimals and numeric text.
    Refuses missing values, booleans, lists and objects, text longer than MAX_NUMBER_TEXT, NaN and
    infinity, absolute values above MAX_MAGNITUDE, and more than MAX_DECIMAL_PLACES decimal places
    (a float such as 0.30000000000000004 is a binary artefact, so floats are rounded to that many places).
    """
    if x is None or (isinstance(x, str) and not x.strip()):
        raise ValueError(f"{_label(what)} is missing")
    if isinstance(x, bool) or not isinstance(x, (int, float, str, Decimal)):
        raise ValueError(f"{_label(what)} must be a number")
    if isinstance(x, int):
        if abs(x) > MAX_MAGNITUDE:
            raise ValueError(f"{_label(what)} is too large: the limit is {MAX_MAGNITUDE:,f}")
        return Decimal(x)
    text = repr(x) if isinstance(x, float) else str(x).strip()
    if len(text) > MAX_NUMBER_TEXT:
        raise ValueError(f"{_label(what)} is too long: write it with at most {MAX_NUMBER_TEXT} characters")
    try:
        d = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{_label(what)} must be a number") from None
    if not d.is_finite():
        raise ValueError(f"{_label(what)} must be a finite number")
    if d != 0 and d.adjusted() > MAX_MAGNITUDE.adjusted() or abs(d) > MAX_MAGNITUDE:
        raise ValueError(f"{_label(what)} is too large: the limit is {MAX_MAGNITUDE:,f}")
    if d.as_tuple().exponent < -MAX_DECIMAL_PLACES:
        if isinstance(x, float):
            d = d.quantize(_QUANTUM, ROUND_HALF_EVEN)
        elif d != 0 and d.adjusted() < -MAX_DECIMAL_PLACES:
            raise ValueError(f"{_label(what)} is too small: use at most {MAX_DECIMAL_PLACES} decimal places")
        elif d != d.quantize(_QUANTUM, ROUND_HALF_EVEN):
            raise ValueError(f"{_label(what)} has more than {MAX_DECIMAL_PLACES} decimal places")
        else:
            d = d.quantize(_QUANTUM)  # trailing zeros only: same value, bounded exponent
    if d.as_tuple().exponent > 0:
        d = d.quantize(Decimal(1))  # "1.5E+3" becomes 1500
    return d + 0 if d == 0 else d  # "-0" becomes "0"


# ---- per-field domains: each takes untrusted input and returns a Decimal or raises ValueError ----------

MAX_PRICE = Decimal("1000000000")
MAX_STARTING_CASH = Decimal("1000000000")


def price_tick(v: Decimal) -> Decimal:
    """The tick size for a price: a cent at or above one dollar, a hundredth of a cent below."""
    return CENT if v >= 1 else TENTH_MILL


def check_price(v: Decimal, what: str = "price") -> Decimal:
    """A positive price on its tick grid, at most MAX_PRICE."""
    if v <= 0:
        raise ValueError(f"{what} must be positive")
    if v > MAX_PRICE:
        raise ValueError(f"{what} is too large: the limit is {MAX_PRICE:,f}")
    if v != v.quantize(price_tick(v)):
        raise ValueError(f"{what} has too many decimal places")
    return v


def parse_price(x, what: str = "price") -> Decimal:
    return check_price(parse(x, what), what)


def parse_cash(x, what: str = "starting cash") -> Decimal:
    """Starting cash of an account: whole cents, more than 0 and at most MAX_STARTING_CASH."""
    v = parse(x, what)
    if v <= 0 or v > MAX_STARTING_CASH:
        raise ValueError(f"{what} must be between 0 and {MAX_STARTING_CASH:,f}")
    if v != v.quantize(CENT):
        raise ValueError(f"{what} must be in whole cents")
    return v


def parse_percent(x, what: str = "percent", low: Decimal = ZERO, high: Decimal = Decimal(100),
                  places: int = 4) -> Decimal:
    """A percentage strictly above `low` and strictly below `high`, with at most `places` decimals."""
    v = parse(x, what)
    if not low < v < high:
        raise ValueError(f"{what} must be above {low:f} and below {high:f}")
    if v != v.quantize(Decimal(1).scaleb(-places)):
        raise ValueError(f"{what} supports at most {places} decimal places")
    return v


def cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, ROUND_HALF_UP)


def px4(x: Decimal) -> Decimal:
    return x.quantize(TENTH_MILL, ROUND_HALF_UP)


def floor_cent(x: Decimal) -> Decimal:
    return x.quantize(CENT, ROUND_FLOOR)


def ceil_cent(x: Decimal) -> Decimal:
    return x.quantize(CENT, ROUND_CEILING)


def model_to_price(x: float) -> Decimal:
    """Convert a pricing-model output (float) into a cent price."""
    return cents(Decimal(repr(float(x))))
