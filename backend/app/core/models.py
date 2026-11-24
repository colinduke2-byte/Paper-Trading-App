"""Typed records shared by every module."""
from dataclasses import dataclass, field, fields
from decimal import Decimal
from typing import Optional

from .money import ZERO, D

BUY, SELL = "buy", "sell"
MARKET, LIMIT, STOP, STOP_LIMIT, TRAILING_STOP = "market", "limit", "stop", "stop_limit", "trailing_stop"
# Auction orders wait for the next regular-session open or close and trade once, at the auction price.
MOO, MOC, LOO, LOC = "market_on_open", "market_on_close", "limit_on_open", "limit_on_close"
OPEN_TYPES, CLOSE_TYPES = (MOO, LOO), (MOC, LOC)
AUCTION_TYPES = OPEN_TYPES + CLOSE_TYPES
MULTILEG = "multileg"  # one order, two to four legs, filled all-or-none at a net price
ORDER_TYPES = (MARKET, LIMIT, STOP, STOP_LIMIT, TRAILING_STOP) + AUCTION_TYPES
DAY, GTC, IOC, FOK, GTD = "day", "gtc", "ioc", "fok", "gtd"
OPG, CLS = "opg", "cls"  # set by the system on auction orders: good for one open / one close only
TIFS = (DAY, GTC, IOC, FOK, GTD)
# Roles of linked child orders. Bracket children and 'exit' children are sized to what their
# parent filled; 'triggered' children keep their own size and need the parent to fill in full.
TAKE_PROFIT, STOP_LOSS, EXIT, TRIGGERED = "take_profit", "stop_loss", "exit", "triggered"

HELD, WORKING, PARTIAL, FILLED, CANCELED, REJECTED, EXPIRED = (
    "held", "working", "partially_filled", "filled", "canceled", "rejected", "expired")
ACTIVE = (HELD, WORKING, PARTIAL)
TERMINAL = (FILLED, CANCELED, REJECTED, EXPIRED)


@dataclass
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    last: Decimal
    last_size: Decimal = ZERO
    volume: Decimal = ZERO
    ts: float = 0.0
    trade: bool = False  # True when this update carries a new print
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    prev_close: Optional[Decimal] = None

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass
class Order:
    id: str
    account_id: str
    symbol: str
    side: str
    qty: Decimal
    type: str = MARKET
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    trail_amount: Optional[Decimal] = None
    trail_percent: Optional[Decimal] = None
    tif: str = DAY
    extended_hours: bool = False
    status: str = WORKING
    filled_qty: Decimal = ZERO
    avg_fill_price: Optional[Decimal] = None
    parent_id: Optional[str] = None
    oco_group: Optional[str] = None
    role: Optional[str] = None  # 'take_profit' / 'stop_loss' / 'exit' / 'triggered' for linked children
    triggered: bool = False
    trail_ref: Optional[Decimal] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    reason: Optional[str] = None
    client_id: Optional[str] = None
    fees: Decimal = ZERO
    expire_at: Optional[float] = None  # good-till-date: the order expires at this time
    good_till: Optional[str] = None  # good-till-date: the date as entered (YYYY-MM-DD)
    auction_at: Optional[float] = None  # on-open / on-close orders: the time of the one auction they wait for
    # Multi-leg orders: [{"symbol", "side", "ratio"}]. `symbol` is then the underlying, `qty` the
    # number of units, `side` is always buy and `limit_price` is the signed net price per unit
    # (positive = net debit, negative = net credit, None = at market).
    legs: Optional[list] = None

    @property
    def remaining(self) -> Decimal:
        return self.qty - self.filled_qty

    @property
    def active(self) -> bool:
        return self.status in ACTIVE


@dataclass
class Fill:
    id: str
    order_id: Optional[str]
    account_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    ts: float
    fee: Decimal = ZERO
    kind: str = "trade"  # trade / exercise / assignment / expiration / closed at expiry


def to_plain(obj) -> dict:
    """Dataclass -> JSON-safe dict with Decimals as strings (lossless, for storage)."""
    out = {}
    for f in fields(obj):
        v = getattr(obj, f.name)
        out[f.name] = str(v) if isinstance(v, Decimal) else v
    return out


def from_plain(cls, data: dict):
    kw = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        v = data[f.name]
        if v is not None and "Decimal" in str(f.type):
            v = D(v)
        kw[f.name] = v
    return cls(**kw)
