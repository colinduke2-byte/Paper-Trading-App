"""Exact decimal helpers. Money and quantities are never floats."""
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING

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


def parse(x) -> Decimal:
    """Parse untrusted API input (JSON numbers arrive as floats) via its string form."""
    if x is None:
        raise ValueError("missing number")
    if isinstance(x, bool):
        raise ValueError("not a number")
    d = Decimal(str(x))
    if not d.is_finite():
        raise ValueError("not a finite number")
    return d


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
