from datetime import date, datetime
from decimal import Decimal

import pytest

from app.core import calendar as cal
from app.core.money import D, parse


def test_money_rejects_floats():
    with pytest.raises(TypeError):
        D(1.1)
    with pytest.raises(TypeError):
        D(True)
    assert D("1.10") + D(1) == Decimal("2.10")
    assert parse(0.1) == Decimal("0.1")
    with pytest.raises(ValueError):
        parse(float("nan"))


@pytest.mark.parametrize("d,open_", [
    (date(2024, 1, 15), False),   # Martin Luther King Jr. Day
    (date(2024, 3, 29), False),   # Good Friday
    (date(2023, 6, 19), False),   # Juneteenth
    (date(2024, 12, 25), False),
    (date(2025, 7, 4), False),
    (date(2026, 7, 3), False),    # Independence Day observed
    (date(2021, 12, 31), True),   # New Year's Day on a Saturday is not observed
    (date(2024, 11, 28), False),  # Thanksgiving
    (date(2024, 11, 29), True),
    (date(2024, 6, 15), False),   # Saturday
    (date(2024, 6, 17), True),
])
def test_trading_days(d, open_):
    assert cal.is_trading_day(d) is open_


@pytest.mark.parametrize("d,early", [(date(2024, 11, 29), True), (date(2025, 7, 3), True), (date(2024, 12, 24), True),
                                     (date(2026, 12, 24), True), (date(2024, 6, 17), False)])
def test_early_closes(d, early):
    assert cal.is_early_close(d) is early


def _ts(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=cal.NY).timestamp()


def test_sessions():
    assert cal.session(_ts(2024, 6, 17, 3, 59)) == "closed"
    assert cal.session(_ts(2024, 6, 17, 4, 0)) == "pre"
    assert cal.session(_ts(2024, 6, 17, 9, 30)) == "regular"
    assert cal.session(_ts(2024, 6, 17, 15, 59)) == "regular"
    assert cal.session(_ts(2024, 6, 17, 16, 0)) == "post"
    assert cal.session(_ts(2024, 6, 17, 20, 0)) == "closed"
    assert cal.session(_ts(2024, 11, 29, 13, 30)) == "post"   # early close
    assert cal.session(_ts(2024, 6, 15, 12, 0)) == "closed"
