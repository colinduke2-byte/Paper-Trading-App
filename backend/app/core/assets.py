"""Asset classes: stock (stocks and funds), option and crypto.

Every module that treats instruments differently asks this one place. A symbol's class is
decided by its form, so it needs no feed lookup and works the same in the risk code, the
ledger, reports and saved files:

- option: an OCC option symbol (see options.symbology), e.g. NVLX261218C00150000
- crypto: a coin quoted in US dollars, written BASE/USD, e.g. SIMCOIN/USD or BTC/USD
- stock: anything else that the feed lists (stocks and exchange-traded funds)

Crypto rules in this simulator (see docs/CRYPTO.md):
- Traded 24 hours a day, 7 days a week, whatever the stock session mode. Day orders expire at
  the UTC day roll. On-open and on-close orders and the extended-hours flag do not apply.
- Fully paid: no margin loan and no short selling, in cash and margin accounts alike. In a
  margin account crypto has no loan value (100% requirement), so it adds nothing to stock
  buying power.
- Settles at once, is not counted for the pattern day trader rule, and has no dividends,
  splits, interest or borrow fees.
- Quantities and prices use each pair's own increments, with up to 9 decimal places. Cash
  is always kept in cents; trade values and fees are rounded to the cent.
"""
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Optional

from ..options.symbology import parse as parse_option

STOCK, OPTION, CRYPTO = "stock", "option", "crypto"
ASSET_CLASSES = (STOCK, OPTION, CRYPTO)
LABELS = {STOCK: "Stock", OPTION: "Option", CRYPTO: "Crypto"}

QUOTE_CURRENCY = "USD"
MAX_PLACES = 9  # the most decimal places any crypto quantity or price may have
FINEST = Decimal(1).scaleb(-MAX_PLACES)
_CRYPTO_RE = re.compile(r"^([A-Z0-9]{1,15})/USD$")


def is_crypto(symbol: str) -> bool:
    return bool(_CRYPTO_RE.match(symbol or ""))


def asset_class(symbol: str) -> str:
    if parse_option(symbol or "") is not None:
        return OPTION
    if is_crypto(symbol):
        return CRYPTO
    return STOCK


def base_currency(symbol: str) -> Optional[str]:
    m = _CRYPTO_RE.match(symbol or "")
    return m.group(1) if m else None


@dataclass(frozen=True)
class CryptoSpec:
    """Order rules for one pair: the provider's minimum order size and increments."""
    symbol: str
    qty_increment: Decimal
    price_increment: Decimal
    min_order_size: Decimal

    @property
    def qty_places(self) -> int:
        return places(self.qty_increment)

    @property
    def price_places(self) -> int:
        return places(self.price_increment)


def places(step: Decimal) -> int:
    """Decimal places of an increment: 0.0001 -> 4, 1 -> 0."""
    return max(0, -step.normalize().as_tuple().exponent)


def on_step(value: Decimal, step: Decimal) -> bool:
    return step > 0 and value % step == 0


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    """The largest multiple of step that is not above value."""
    return ((value / step).to_integral_value(ROUND_FLOOR) * step).quantize(step)


def round_to(value: Decimal, step: Decimal) -> Decimal:
    return ((value / step).to_integral_value(ROUND_HALF_UP) * step).quantize(step)


def _dec(v, default: str) -> Decimal:
    try:
        d = Decimal(str(v)) if v not in (None, "") else Decimal(default)
    except (ArithmeticError, ValueError):
        d = Decimal(default)
    if not d.is_finite() or d <= 0:
        d = Decimal(default)
    return max(d, FINEST)


def spec_from_info(symbol: str, info: Optional[dict]) -> CryptoSpec:
    """The order rules stored in a feed's symbol record (strings, as the provider lists them)."""
    info = info or {}
    return CryptoSpec(symbol, _dec(info.get("qty_increment"), "0.000000001"),
                      _dec(info.get("price_increment"), "0.000000001"), _dec(info.get("min_order_size"), "0.000000001"))


def crypto_info(symbol: str, name: str, qty_increment: str, price_increment: str, min_order_size: str,
                **extra) -> dict:
    """A feed symbol record for a crypto pair. Increments are kept as decimal strings."""
    return {"symbol": symbol, "name": name, "sector": "Crypto", "type": CRYPTO, "asset_class": CRYPTO,
            "base": base_currency(symbol), "quote_currency": QUOTE_CURRENCY,
            "qty_increment": str(qty_increment), "price_increment": str(price_increment),
            "min_order_size": str(min_order_size), **extra}
