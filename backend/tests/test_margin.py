from app.risk import margin
from helpers import D

CALL = "ABC261218C00110000"
PUT = "ABC261218P00090000"


def test_long_stock_reg_t():
    pos, marks = {"ABC": D(200)}, {"ABC": D("100")}
    assert margin.requirement(pos, marks) == D("10000")
    assert margin.requirement(pos, marks, maintenance=True) == D("5000")
    # bought 200 @ 100 with 10,000 cash -> cash is -10,000, equity 10,000: exactly at the initial requirement
    ev = margin.evaluate("margin", D("-10000"), pos, marks)
    assert ev.ok and ev.excess == 0
    assert not margin.evaluate("margin", D("-10000.01"), pos, marks).ok


def test_buying_power_is_twice_excess_equity():
    assert margin.buying_power("margin", D("50000"), {}, {})["stock"] == D("100000")
    assert margin.buying_power("cash", D("50000"), {}, {})["stock"] == D("50000")


def test_short_stock():
    pos, marks = {"ABC": D(-100)}, {"ABC": D("50")}
    # sold 100 @ 50 from 10,000 cash -> cash 15,000, equity 10,000
    ev = margin.evaluate("margin", D("15000"), pos, marks)
    assert ev.equity == D("10000") and ev.requirement == D("2500")
    assert margin.requirement(pos, marks, maintenance=True) == D("1500")
    assert not margin.evaluate("cash", D("15000"), pos, marks).ok


def test_margin_call_threshold():
    # 200 shares bought at 100 with a 10,000 loan; maintenance 25%: call below 66.67
    for px, safe in ((D("66.67"), True), (D("66.66"), False)):
        pos, marks = {"ABC": D(200)}, {"ABC": px}
        eq = margin.equity(D("-10000"), pos, marks)
        assert (eq >= margin.requirement(pos, marks, maintenance=True)) is safe


def test_long_options_paid_in_full():
    pos, marks = {CALL: D(3)}, {CALL: D("2.50"), "ABC": D("100")}
    assert margin.requirement(pos, marks) == D("750")
    assert margin.evaluate("cash", D("0"), pos, marks).ok


def test_naked_call_and_put():
    marks = {CALL: D("1.50"), PUT: D("1.20"), "ABC": D("100")}
    # call: premium 1.50 + max(20 - 10 OTM, 10) = 11.50 x 100
    assert margin.requirement({CALL: D(-1)}, marks) == D("1150")
    # put: premium 1.20 + max(20 - 10 OTM, 10% of strike 9) = 11.20 x 100
    assert margin.requirement({PUT: D(-1)}, marks) == D("1120")


def test_covered_call_needs_only_stock_margin():
    marks = {CALL: D("1.50"), "ABC": D("100")}
    assert margin.requirement({"ABC": D(100), CALL: D(-1)}, marks) == D("5000")
    # two calls against 100 shares: one covered, one naked
    assert margin.requirement({"ABC": D(100), CALL: D(-2)}, marks) == D("6150")
    assert margin.evaluate("cash", D("0"), {"ABC": D(100), CALL: D(-1)}, marks).ok
    assert not margin.evaluate("cash", D("0"), {"ABC": D(100), CALL: D(-2)}, marks).ok


def test_cash_secured_put():
    marks = {PUT: D("1.20"), "ABC": D("100")}
    assert margin.evaluate("cash", D("9000"), {PUT: D(-1)}, marks).ok
    assert not margin.evaluate("cash", D("8999.99"), {PUT: D(-1)}, marks).ok
