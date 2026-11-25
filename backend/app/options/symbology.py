"""OCC option symbols, e.g. NVLX261218C00150000 = NVLX, 18 Dec 2026, call, strike 150."""
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from typing import Optional

from ..core import calendar as cal

_RE = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
MAX_SYMBOL = 21  # the longest OCC symbol: a 6-letter root, YYMMDD, C or P and 8 strike digits


@dataclass(frozen=True)
class Contract:
    symbol: str
    underlying: str
    expiry: date
    kind: str  # 'C' or 'P'
    strike: Decimal

    @property
    def expiry_ts(self) -> float:
        return cal.close_ts(self.expiry)


def parse(symbol: str) -> Optional[Contract]:
    """The contract an OCC symbol names, or None. Anything longer than an OCC symbol is refused before
    the cache, so untrusted input (a request path, a WebSocket list) cannot fill it with large strings."""
    if not isinstance(symbol, str) or len(symbol) > MAX_SYMBOL:
        return None
    return _parse(symbol)


@lru_cache(maxsize=20000)
def _parse(symbol: str) -> Optional[Contract]:
    m = _RE.match(symbol)
    if not m:
        return None
    root, yy, mm, dd, kind, strike = m.groups()
    try:
        expiry = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    return Contract(symbol, root, expiry, kind, Decimal(int(strike)) / 1000)


def build(underlying: str, expiry: date, kind: str, strike: Decimal) -> str:
    return f"{underlying}{expiry:%y%m%d}{kind}{int(strike * 1000):08d}"


def multiplier(symbol: str) -> int:
    return 100 if parse(symbol) else 1


def describe(symbol: str) -> str:
    c = parse(symbol)
    if not c:
        return symbol
    return f"{c.underlying} {c.expiry:%d %b %Y} {c.strike.normalize():f} {'Call' if c.kind == 'C' else 'Put'}"
