"""Business days for both session modes.

- "us": the US equity calendar (`core.calendar`); the date is the New York date.
- "always": the simulated market never closes, so every UTC day is a business day.
"""
from datetime import date, timedelta

from ..core import calendar as cal

ONE_DAY = timedelta(days=1)


class Days:
    def __init__(self, mode: str = "always"):
        self.mode = mode

    def today(self, ts: float) -> date:
        return cal.trading_day(ts, "us" if self.mode == "us" else "always")

    def is_business_day(self, d: date) -> bool:
        return True if self.mode != "us" else cal.is_trading_day(d)

    def add(self, d: date, n: int) -> date:
        """The n-th business day after d (n >= 1)."""
        while n > 0:
            d += ONE_DAY
            if self.is_business_day(d):
                n -= 1
        return d

    def current_or_previous(self, d: date) -> date:
        while not self.is_business_day(d):
            d -= ONE_DAY
        return d

    def window_start(self, d: date, days: int) -> date:
        """First date of the rolling window of `days` business days that ends on (or before) d."""
        d = self.current_or_previous(d)
        for _ in range(days - 1):
            d -= ONE_DAY
            d = self.current_or_previous(d)
        return d
