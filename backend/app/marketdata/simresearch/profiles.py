"""Seeded building blocks shared by the simulated research generators.

Everything here is a pure function of (seed, symbol, date or index). Model arithmetic is in
floats; the statement and calendar builders convert to Decimal at the edge.
"""
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache

from ...core import calendar as cal

SOURCE = "Simulated"

# sector: gross margin, net margin (profitable companies), annual revenue growth, asset turnover,
# debt/equity, capex/revenue, beta, price/sales for loss-making companies, seasonality by quarter
SECTORS = {
    "Technology": (0.62, 0.22, 0.12, 0.60, 0.35, 0.06, 1.20, 7.0, (0.95, 0.98, 1.00, 1.07)),
    "Health Care": (0.68, 0.18, 0.07, 0.50, 0.55, 0.04, 0.90, 9.0, (0.98, 1.00, 0.99, 1.03)),
    "Financials": (0.92, 0.24, 0.05, 0.12, 1.40, 0.02, 1.05, 3.0, (1.00, 1.01, 0.99, 1.00)),
    "Energy": (0.34, 0.11, 0.03, 0.70, 0.45, 0.12, 1.10, 1.5, (1.02, 0.98, 1.01, 0.99)),
    "Consumer": (0.40, 0.08, 0.06, 1.20, 0.70, 0.05, 1.00, 2.0, (0.92, 0.97, 1.00, 1.11)),
    "Industrials": (0.31, 0.10, 0.05, 0.80, 0.80, 0.06, 1.05, 2.0, (0.96, 1.02, 1.00, 1.02)),
    "Utilities": (0.45, 0.13, 0.03, 0.25, 1.35, 0.25, 0.55, 2.0, (1.04, 0.94, 1.08, 0.94)),
    "Communication": (0.55, 0.15, 0.07, 0.50, 0.90, 0.10, 1.00, 4.0, (0.97, 0.99, 0.99, 1.05)),
}
DEFAULT_SECTOR = SECTORS["Industrials"]
TAX_RATE = 0.21
ANCHOR_QUARTER = 2026 * 4 + 1  # 2026 Q2: the listed EPS is the sum of the four quarters ending here
NOISE_EPS = 0.07  # quarterly profit noise (log); drives beats and misses
NOISE_EST = 0.03  # analyst estimate error (log)
ESTIMATE_BIAS = 0.015  # estimates lean low, so a little more than half of reports beat


def rng(seed, *parts) -> random.Random:
    """A generator that depends only on the seed and the parts (string seeding is stable across runs)."""
    return random.Random(":".join(str(p) for p in (seed, *parts)))


def ny_ts(d: date, t: time) -> float:
    return datetime.combine(d, t, tzinfo=cal.NY).timestamp()


def business_on_or_after(d: date) -> date:
    while not cal.is_trading_day(d):
        d += timedelta(days=1)
    return d


def quarter_end(k: int) -> date:
    y, q = divmod(k, 4)
    m = 3 * (q + 1)
    return date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def quarter_of(d: date) -> int:
    return d.year * 4 + (d.month - 1) // 3


def quarter_label(k: int) -> str:
    return f"Q{k % 4 + 1} {k // 4}"


@dataclass(frozen=True)
class Profile:
    symbol: str
    name: str
    sector: str
    etf: bool
    gross_margin: float
    net_margin: float  # negative for a loss-making company
    growth: float
    asset_turnover: float
    debt_equity: float
    other_liab: float  # other liabilities / equity
    capex: float
    beta: float
    season: tuple
    report_offset: int  # days after quarter end the report usually lands
    hour: str  # "before open" | "after close"
    expected_move: float  # standard deviation of the earnings-day gap (log return)
    cash_frac: float
    ar_frac: float
    inv_frac: float
    ppe_frac: float
    ap_frac: float


@lru_cache(maxsize=512)
def profile(seed: int, symbol: str, name: str, sector: str, vol: float, eps: float, price: float,
            shares: float, etf: bool) -> Profile:
    gm, nm, g, turn, de, capex, beta, ps, season = SECTORS.get(sector, DEFAULT_SECTOR)
    r = rng(seed, symbol, "profile")
    jit = lambda lo, hi: r.uniform(lo, hi)
    net = nm * jit(0.7, 1.3)
    if eps <= 0 and not etf:
        revenue = max(price * shares / (ps * jit(0.7, 1.3)), 1e6)
        net = (eps * shares) / revenue  # negative: the listed loss on that revenue
    inv = {"Consumer": 0.14, "Industrials": 0.12, "Energy": 0.05, "Technology": 0.06, "Health Care": 0.07}.get(sector, 0.0)
    return Profile(
        symbol=symbol, name=name, sector=sector, etf=etf,
        gross_margin=min(gm * jit(0.9, 1.1), 0.97), net_margin=net, growth=g * jit(0.4, 1.6) + (0.08 if eps <= 0 else 0),
        asset_turnover=turn * jit(0.8, 1.25), debt_equity=de * jit(0.6, 1.4), other_liab=jit(0.35, 0.8),
        capex=capex * jit(0.8, 1.2), beta=round(beta * jit(0.8, 1.25), 2), season=season,
        report_offset=18 + r.randrange(0, 22), hour="before open" if r.random() < 0.5 else "after close",
        expected_move=vol / math.sqrt(252) * jit(2.2, 3.4),
        cash_frac=jit(0.06, 0.16), ar_frac=jit(0.06, 0.14), inv_frac=inv * jit(0.7, 1.3), ppe_frac=jit(0.15, 0.35),
        ap_frac=jit(0.04, 0.10))


def release_time(seed: int, p: Profile, k: int):
    """(date, unix time) the results for fiscal quarter k are published. Fourth quarters come later."""
    r = rng(seed, p.symbol, "release", k)
    days = p.report_offset + (10 if k % 4 == 3 else 0) + r.randint(-3, 3)
    d = business_on_or_after(quarter_end(k) + timedelta(days=days))
    return d, ny_ts(d, time(8, 0) if p.hour == "before open" else time(16, 5))
