"""Exact decimal helpers. Money and quantities are never floats."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING

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


def parse(x, what: str = "value") -> Decimal:
    """Parse untrusted API input (JSON numbers arrive as floats) via its string form.

    Raises ValueError naming ``what`` (for example "starting cash") for anything that is not a plain
    finite number: text, lists, objects, true/false, NaN, and numbers with thousands separators."""
    if x is None:
        raise ValueError(f"{what} is missing")
    if isinstance(x, bool) or not isinstance(x, (int, float, str, Decimal)):
        raise ValueError(f"{what} must be a number, not {_kind(x)}")
    text = str(x).strip()
    try:
        d = Decimal(text)
    except (InvalidOperation, ValueError):
        hint = " Write it without thousands separators, for example 1000." if "," in text and \
            _number_with_commas(text) else ""
        raise ValueError(f"{what} is not a number: {_show(text)}.{hint}") from None
    if not d.is_finite():
        raise ValueError(f"{what} must be a finite number")
    return d


def _number_with_commas(text: str) -> bool:
    try:
        Decimal(text.replace(",", ""))
        return True
    except (InvalidOperation, ValueError):
        return False


def _show(text: str) -> str:
    return repr(text if len(text) <= 40 else text[:40] + "...")


def _kind(x) -> str:
    if isinstance(x, bool):
        return "true/false"
    return {list: "a list", tuple: "a list", dict: "an object"}.get(type(x), type(x).__name__)


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
