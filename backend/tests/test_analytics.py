"""Reference cases for the analytics package. Every expected value is worked out by hand in the comments."""
from datetime import datetime
from decimal import Decimal

import pytest

from app.analytics import returns as R
from app.analytics.common import NY, anniversary, equity_points, external_flows, months_between, parse_month
from app.analytics.statement import monthly_statement, statement_html
from app.analytics.trades import closed_lots, exposure, gains_report, trade_stats
from app.core.models import Fill
from app.ledger.account import Account
from helpers import D

OPT = "ABC261218C00100000"


def ts(y, m, d, h=12, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=NY).timestamp()


def fill(i, side, qty, price, symbol="ABC", fee="0", t=None):
    return Fill(id=f"F{i}", order_id=f"O{i}", account_id="A1", symbol=symbol, side=side, qty=D(str(qty)),
                price=D(str(price)), ts=float(i if t is None else t), fee=D(fee))


def close(x, y, tol="1e-9"):
    return abs(D(x) - D(y)) <= D(tol)


# ---- returns ----------------------------------------------------------------------------------
def test_time_weighted_return_ignores_deposits():
    # r1 = 110/100 - 1 = 0.10; a 50 deposit arrives before t=2, so r2 = 165/(110+50) - 1 = 0.03125.
    # TWR = 1.10 * 1.03125 - 1 = 0.134375 (a simple 165/100 - 1 would wrongly say 65 %).
    series = [(0.0, D("100")), (1.0, D("110")), (2.0, D("165"))]
    assert R.time_weighted_return(series, [(1.5, D("50"))]) == D("0.134375")
    # a flow at or before the first point is already inside it
    assert R.time_weighted_return(series[:2], [(0.0, D("100"))]) == D("0.1")
    assert R.time_weighted_return(series[:1]) is None


def test_daily_returns_volatility_and_sharpe():
    # Daily values 100, 102, 99.96, 101.9592: returns +2 %, -2 %, +2 %.
    # mean = 0.02/3; sample variance = 0.0016/3, so sd = 0.04/sqrt(3).
    # volatility = sd * sqrt(252) = 0.04 * sqrt(84) = 0.36660606...
    # Sharpe = mean/sd * sqrt(252) = sqrt(3)/6 * sqrt(252) = sqrt(756)/6 = 4.58257569...
    series = [(ts(2026, 11, 2), D("100")), (ts(2026, 11, 3), D("102")), (ts(2026, 11, 4), D("99.96")),
              (ts(2026, 11, 5), D("101.9592"))]
    rets = R.daily_returns(series)
    assert [r for _, r in rets] == [D("0.02"), D("-0.02"), D("0.02")]
    assert [d.isoformat() for d, _ in rets] == ["2026-11-03", "2026-11-04", "2026-11-05"]
    vals = [r for _, r in rets]
    assert close(R.volatility(vals), "0.366606055596")
    assert close(R.sharpe(vals), "4.582575694956")
    # With a 2.52 % risk-free rate the daily hurdle is 0.0001: (0.02/3 - 0.0001)/sd * sqrt(252) = 4.51384...
    assert close(R.sharpe(vals, D("0.0252")), (D("0.02") / 3 - D("0.0001")) / (D("0.04") / D(3).sqrt()) * D(252).sqrt())
    assert R.volatility(vals[:1]) is None and R.sharpe(vals[:1]) is None
    assert R.sharpe([D("0.01"), D("0.01")]) is None  # no variation, no ratio


def test_intraday_points_collapse_to_one_close_per_day():
    series = [(ts(2026, 11, 2, 9), D("100")), (ts(2026, 11, 2, 15), D("150")), (ts(2026, 11, 2, 16), D("101")),
              (ts(2026, 11, 3, 10), D("90")), (ts(2026, 11, 3, 15), D("111.1"))]
    # opening 100 -> day 1 close 101 (+1 %) -> day 2 close 111.1 (+10 %)
    assert [r for _, r in R.daily_returns(series)] == [D("0.01"), D("0.1")]


def test_max_drawdown_with_dates():
    # index 1, 1.2, 0.9, 1.0, 1.25, 1.1: worst fall is 1.2 -> 0.9 = 25 %, recovered at t=4 (1.25 >= 1.2).
    idx = [(float(i), D(v)) for i, v in enumerate(["1", "1.2", "0.9", "1.0", "1.25", "1.1"])]
    dd = R.max_drawdown(idx)
    assert dd == {"max_drawdown": D("0.25"), "peak_at": 1.0, "trough_at": 2.0, "recovered_at": 4.0}
    # not yet recovered
    assert R.max_drawdown(idx[:4])["recovered_at"] is None
    # a later, smaller drawdown does not replace the larger one
    assert R.max_drawdown(idx + [(6.0, D("1.0"))])["max_drawdown"] == D("0.25")
    assert R.max_drawdown([(0.0, D("1")), (1.0, D("2"))])["max_drawdown"] == 0


def test_drawdown_is_measured_on_returns_not_on_deposits():
    # 100 -> 90 is a 10 % loss; a 100 deposit then lifts value to 190 but the return is 190/(90+100) - 1 = 0.
    series = [(0.0, D("100")), (1.0, D("90")), (2.0, D("190"))]
    rep = R.report(series, [(1.5, D("100"))])
    assert rep["max_drawdown"] == D("0.1") and rep["drawdown_recovered_at"] is None
    assert rep["time_weighted_return"] == D("-0.1") and rep["net_deposits"] == D("100")


def test_benchmark_comparison():
    # Benchmark 50 until t=10, then 55: over the account's period (0..12) it returns 10 %.
    # The account goes 100 -> 104 (+4 %), so the excess return is -6 %.
    hist = [(0.0, D("50")), (10.0, D("55"))]
    assert R.price_at(hist, 9.99) == D("50") and R.price_at(hist, 10) == D("55") and R.price_at(hist, -5) == D("50")
    rep = R.report([(0.0, D("100")), (5.0, D("102")), (12.0, D("104"))], [], hist)
    b = rep["benchmark"]
    assert b["total_return"] == D("0.1") and b["excess_return"] == D("-0.06")
    assert [v for _, v in b["index"]] == [D("1"), D("1"), D("1.1")]


def test_beta_against_benchmark():
    # Account daily returns are exactly twice the benchmark's: beta 2, correlation 1.
    days = [ts(2026, 11, d) for d in (2, 3, 4, 5)]
    bench = [(days[0], D("100")), (days[1], D("101")), (days[2], D("100.495")), (days[3], D("102.5049"))]
    # benchmark: +1 %, -0.5 %, +2 %; account: +2 %, -1 %, +4 %
    acct = [(days[0], D("100")), (days[1], D("102")), (days[2], D("100.98")), (days[3], D("105.0192"))]
    b = R.report(acct, [], bench)["benchmark"]
    assert close(b["beta"], 2) and close(b["correlation"], 1)


def test_equity_points_and_flows_from_account():
    a = Account("A1", "t", "margin", D("1000"), 10.0)
    a.equity_history = [[60, "1010"], [120, "990"]]
    pts = equity_points(a, 130.0, D("995"))
    assert pts == [(10.0, D("1000")), (60.0, D("1010")), (120.0, D("990")), (130.0, D("995"))]
    assert external_flows(a) == [(10.0, D("1000"))]


# ---- trade statistics -------------------------------------------------------------------------
def stats_account():
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.apply_fill(fill(1, "buy", 10, "100", t=0), 1)
    a.apply_fill(fill(2, "sell", 10, "110", t=3600), 1)      # long ABC +100, held 1 h
    a.apply_fill(fill(3, "buy", 5, "100", t=7200), 1)
    a.apply_fill(fill(4, "sell", 5, "90", t=14400), 1)       # long ABC -50, held 2 h
    a.apply_fill(fill(5, "sell", 2, "50", "XYZ", t=0), 1)
    a.apply_fill(fill(6, "buy", 2, "40", "XYZ", t=10800), 1)  # short XYZ +20, held 3 h
    a.apply_fill(fill(7, "buy", 1, "2", OPT, t=0), 100)
    a.apply_fill(fill(8, "sell", 1, "1", OPT, t=7200), 100)   # long option -100, held 2 h
    return a


def test_trade_statistics_reference():
    a = stats_account()
    s = trade_stats(closed_lots(a))
    o = s["overall"]
    # gains +100, -50, +20, -100
    assert (o["trades"], o["wins"], o["losses"], o["scratches"]) == (4, 2, 2, 0)
    assert o["win_rate"] == D("0.5")
    assert o["average_win"] == D("60.00") and o["average_loss"] == D("-75.00")
    assert o["profit_factor"] == D("0.8")              # 120 / 150
    assert o["expectancy"] == D("-7.50")              # -30 / 4
    assert o["largest_win"] == D("100") and o["largest_loss"] == D("-100")
    assert o["average_holding_seconds"] == 7200       # (1 + 2 + 3 + 2) h / 4
    assert o["total_gain"] == a.realized == D("-30")
    assert set(s["by_symbol"]) == {"ABC", "XYZ", OPT} and s["by_symbol"]["ABC"]["total_gain"] == D("50")
    assert s["by_direction"]["short"]["trades"] == 1 and s["by_direction"]["short"]["total_gain"] == D("20")
    assert s["by_direction"]["long"]["profit_factor"] == D("0.6666666667")  # 100 / 150
    assert s["by_asset"]["option"]["total_gain"] == D("-100") and s["by_asset"]["option"]["win_rate"] == 0
    assert s["by_asset"]["stock"]["profit_factor"] == D("2.4")  # 120 / 50


def test_trade_statistics_empty_and_no_losses():
    o = trade_stats([])["overall"]
    assert o["trades"] == 0 and o["win_rate"] is None and o["profit_factor"] is None and o["expectancy"] is None
    a = Account("A1", "t", "margin", D("1000"), 0)
    a.apply_fill(fill(1, "buy", 1, "10"), 1)
    a.apply_fill(fill(2, "sell", 1, "10"), 1)
    o = trade_stats(closed_lots(a))["overall"]
    assert o["scratches"] == 1 and o["win_rate"] == 0 and o["profit_factor"] is None


# ---- tax-lot methods --------------------------------------------------------------------------
def lot_account(method):
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.lot_method = method
    a.apply_fill(fill(1, "buy", 10, "100"), 1)
    a.apply_fill(fill(2, "buy", 10, "120"), 1)
    a.apply_fill(fill(3, "buy", 10, "90"), 1)
    a.apply_fill(fill(4, "sell", 15, "110"), 1)
    return a


@pytest.mark.parametrize("method,gain,lots,basis", [
    # FIFO: 10 @100 (+100) then 5 of @120 (-50) = +50; left 5 @120 + 10 @90 = 1500
    ("fifo", "50", [("100", "10"), ("120", "5")], "1500"),
    # LIFO: 10 @90 (+200) then 5 of @120 (-50) = +150; left 10 @100 + 5 @120 = 1600
    ("lifo", "150", [("90", "10"), ("120", "5")], "1600"),
    # Highest cost: 10 @120 (-100) then 5 of @100 (+50) = -50; left 5 @100 + 10 @90 = 1400
    ("highest_cost", "-50", [("120", "10"), ("100", "5")], "1400"),
])
def test_lot_methods_give_different_gains(method, gain, lots, basis):
    a = lot_account(method)
    assert a.realized == D(gain)
    assert [(c["open_price"], c["qty"]) for c in a.closed] == lots
    assert a.cost_basis("ABC") == D(basis) and a.position("ABC") == 15
    # the cash is the same whichever lots are closed: 100000 - 3100 + 1650
    assert a.cash == D("98550")
    assert gains_report(a)["totals"]["gain"] == D(gain)
    assert all(c["lot_method"] == method for c in a.closed)


def test_highest_cost_on_a_short_closes_the_cheapest_sale():
    # short 10 @50 and 10 @40, cover 10 @45. Highest cost (smallest gain) covers the @40 lot: 400 - 450 = -50.
    # FIFO covers the @50 lot: 500 - 450 = +50.
    for method, gain in (("highest_cost", "-50"), ("fifo", "50"), ("lifo", "-50")):
        a = Account("A1", "t", "margin", D("100000"), 0)
        a.lot_method = method
        a.apply_fill(fill(1, "sell", 10, "50"), 1)
        a.apply_fill(fill(2, "sell", 10, "40"), 1)
        assert a.apply_fill(fill(3, "buy", 10, "45"), 1) == D(gain)


def test_highest_cost_ties_go_to_the_oldest_lot():
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.lot_method = "highest_cost"
    for i in (1, 2):
        a.apply_fill(fill(i, "buy", 1, "100"), 1)
    a.apply_fill(fill(3, "sell", 1, "100"), 1)
    assert a.closed[0]["open_fill_id"] == "F1"


def test_lot_method_and_journal_survive_storage_and_old_files_load():
    a = lot_account("lifo")
    a.journal.append({"id": "J1", "target_type": "trade", "target_id": "C1", "note": "n", "tags": [], "created_at": 1, "updated_at": 1})
    b = Account.from_plain(a.to_plain())
    assert b.lot_method == "lifo" and b.journal == a.journal
    assert [l.fill_id for l in b.lots["ABC"]] == [l.fill_id for l in a.lots["ABC"]] == ["F1", "F2"]
    # A state file written before this change: no lot_method, no journal, four-field lots, short closed entries.
    old = a.to_plain()
    del old["lot_method"], old["journal"]
    old["lots"] = {s: [l[:4] for l in ls] for s, ls in old["lots"].items()}
    new_keys = ("id", "basis", "close_amount", "open_fill_id", "close_fill_id", "order_id", "kind", "lot_method")
    old["closed"] = [{k: v for k, v in c.items() if k not in new_keys} for c in old["closed"]]
    c = Account.from_plain(old)
    assert c.lot_method == "fifo" and c.journal == [] and c.lots["ABC"][0].fill_id == ""
    assert c.positions() == a.positions() and c.cash == a.cash
    # derived proceeds and cost for old entries match the recorded ones exactly
    strip = lambda rows: [{k: r[k] for k in ("id", "proceeds", "cost", "gain")} for r in rows]
    assert strip(closed_lots(c)) == strip(closed_lots(a))
    c.apply_fill(fill(9, "sell", 15, "100"), 1)  # old lots still close
    assert c.position("ABC") == 0


# ---- realised gains report --------------------------------------------------------------------
def test_short_and_long_term():
    assert anniversary(datetime(2024, 2, 29).date()).isoformat() == "2025-02-28"
    cases = [((2025, 1, 15), (2026, 1, 15), "short"), ((2025, 1, 15), (2026, 1, 16), "long"),
             ((2024, 2, 29), (2025, 2, 28), "short"), ((2024, 2, 29), (2025, 3, 1), "long")]
    for i, (o, c, term) in enumerate(cases):
        a = Account("A1", "t", "margin", D("100000"), 0)
        a.apply_fill(fill(1, "buy", 1, "100", t=ts(*o)), 1)
        a.apply_fill(fill(2, "sell", 1, "150", t=ts(*c)), 1)
        assert gains_report(a)["rows"][0]["term"] == term, (o, c)
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.apply_fill(fill(1, "sell", 1, "100", t=ts(2024, 1, 2)), 1)
    a.apply_fill(fill(2, "buy", 1, "50", t=ts(2026, 1, 2)), 1)
    r = gains_report(a)
    assert r["rows"][0]["term"] == "short" and r["totals"]["short_term"] == D("50")


def test_gains_report_amounts():
    a = stats_account()
    rep = gains_report(a)
    rows = {r["id"]: r for r in rep["rows"]}
    # long ABC: sold 10 @110 for 1100, cost 1000
    assert (rows["C1"]["proceeds"], rows["C1"]["cost"], rows["C1"]["gain"]) == (D("1100"), D("1000"), D("100"))
    # short XYZ: opening sale 100 is the proceeds, closing purchase 80 the cost
    assert (rows["C3"]["proceeds"], rows["C3"]["cost"], rows["C3"]["gain"]) == (D("100"), D("80"), D("20"))
    # option with multiplier 100: 1 x 1 x 100 = 100 proceeds, 200 cost
    assert (rows["C4"]["proceeds"], rows["C4"]["cost"]) == (D("100"), D("200"))
    t = rep["totals"]
    assert t["gain"] == a.realized and t["proceeds"] - t["cost"] == t["gain"]


def wash_account(rebuy_day):
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.apply_fill(fill(1, "buy", 10, "100", t=ts(2026, 3, 1)), 1)
    a.apply_fill(fill(2, "sell", 10, "90", t=ts(2026, 3, 11)), 1)  # loss of 100
    if rebuy_day is not None:
        a.apply_fill(fill(3, "buy", 10, "91", t=ts(2026, 3, 11) + rebuy_day * 86400), 1)
    return a


@pytest.mark.parametrize("rebuy_day,flag", [(None, False), (5, True), (30, True), (31, False), (45, False)])
def test_wash_sale_after(rebuy_day, flag):
    r = gains_report(wash_account(rebuy_day))
    assert r["rows"][0]["wash_sale"] is flag
    assert r["totals"]["wash_sale_lots"] == int(flag)
    assert any("informational" in n for n in r["notes"])


def test_wash_sale_before_and_not_for_gains_or_other_symbols():
    # buy 10 @100 on 1 Mar, buy 10 more @95 on 20 Mar, sell the first lot (FIFO) @90 on 25 Mar:
    # the 20 Mar purchase is 5 days before the loss, so it is flagged.
    a = Account("A1", "t", "margin", D("100000"), 0)
    a.apply_fill(fill(1, "buy", 10, "100", t=ts(2026, 3, 1)), 1)
    a.apply_fill(fill(2, "buy", 10, "95", t=ts(2026, 3, 20)), 1)
    a.apply_fill(fill(3, "sell", 10, "90", t=ts(2026, 3, 25)), 1)
    row = gains_report(a)["rows"][0]
    assert row["wash_sale"] and row["wash_sale_with"][0]["fill_id"] == "F2"
    # a gain is never a wash sale; nor does buying a different symbol count
    b = Account("A1", "t", "margin", D("100000"), 0)
    b.apply_fill(fill(1, "buy", 10, "100", t=ts(2026, 3, 1)), 1)
    b.apply_fill(fill(2, "buy", 10, "100", "XYZ", t=ts(2026, 3, 5)), 1)
    b.apply_fill(fill(3, "sell", 10, "90", t=ts(2026, 3, 10)), 1)
    b.apply_fill(fill(4, "buy", 10, "80", t=ts(2026, 3, 12)), 1)
    b.apply_fill(fill(5, "sell", 10, "85", t=ts(2026, 3, 13)), 1)
    flags = [(r["gain"] < 0, r["wash_sale"]) for r in gains_report(b)["rows"]]
    assert flags == [(True, True), (False, False)]
    c = Account("A1", "t", "margin", D("100000"), 0)
    c.apply_fill(fill(1, "buy", 10, "100", t=ts(2026, 3, 1)), 1)
    c.apply_fill(fill(2, "buy", 10, "100", "XYZ", t=ts(2026, 3, 5)), 1)
    c.apply_fill(fill(3, "sell", 10, "90", t=ts(2026, 3, 10)), 1)
    assert not gains_report(c)["rows"][0]["wash_sale"]


def test_gains_report_month_filter():
    a = wash_account(None)
    start, end = ts(2026, 3, 1, 0), ts(2026, 4, 1, 0)
    assert len(gains_report(a, start, end)["rows"]) == 1
    assert gains_report(a, end, None)["rows"] == []


# ---- exposure ---------------------------------------------------------------------------------
def test_exposure_reference():
    rows = [{"symbol": "AAA", "market_value": D("6000")}, {"symbol": "BBB", "market_value": D("-2000")},
            {"symbol": "CCC", "market_value": D("1000")}, {"symbol": "AAA261218C00100000", "market_value": D("500")}]
    e = exposure(rows, {"AAA": "Technology", "BBB": "Technology", "CCC": "Energy"}, D("10000"))
    t = e["totals"]
    # long 6000 + 1000 + 500 = 7500; short -2000; gross 9500; net 5500
    assert (t["long"], t["short"], t["gross"], t["net"]) == (D("7500"), D("-2000"), D("9500"), D("5500"))
    assert t["gross_leverage"] == D("0.95") and t["net_leverage"] == D("0.55")
    tech = e["by_sector"][0]
    assert tech["sector"] == "Technology" and (tech["long"], tech["short"], tech["net"], tech["gross"]) == (
        D("6500"), D("-2000"), D("4500"), D("8500"))
    assert tech["net_weight"] == D("0.45")
    assert e["by_symbol"][0]["symbol"] == "AAA" and e["by_symbol"][0]["weight"] == D("0.6")
    assert [s["sector"] for s in e["by_sector"]] == ["Technology", "Energy"]


# ---- monthly statement ------------------------------------------------------------------------
def statement_account():
    a = Account("A1", "Test <b>account</b>", "margin", D("10000"), ts(2026, 10, 15))
    a.apply_fill(fill(1, "buy", 10, "100", fee="1", t=ts(2026, 10, 20)), 1)
    a.equity_history = [[int(ts(2026, 10, 31, 23)), "10049"]]       # marked at 105: 8999 + 1050
    a.apply_fill(fill(2, "sell", 10, "110", fee="1", t=ts(2026, 11, 5)), 1)
    a.equity_history.append([int(ts(2026, 11, 30, 23)), "10098"])
    return a


def test_statement_past_month_reconciles():
    a = statement_account()
    st = monthly_statement(a, 2026, 11, ts(2026, 12, 2), marks={})
    assert not st["current"]
    assert st["opening"]["cash"] == D("8999") and st["opening"]["cost_basis"] == D("1000")
    assert st["opening"]["account_value"] == D("10049") and st["opening"]["unrealized_pnl"] == D("50")
    assert st["closing"]["cash"] == D("10098") and st["closing"]["account_value"] == D("10098")
    assert st["closing"]["unrealized_pnl"] == 0 and st["positions"] == []
    assert st["realized_pnl"] == D("100") and st["fees"] == D("1") and st["deposits_total"] == 0
    assert st["trade_count"] == 1 and st["sold"] == D("1100") and st["trades"][0]["net_cash"] == D("1099")
    # 10049 + 0 + 100 - 1 + 0 + (0 - 50) = 10098
    assert st["unrealized_change"] == D("-50") and st["reconciliation_difference"] == 0


def test_statement_opening_month_and_positions():
    a = statement_account()
    st = monthly_statement(a, 2026, 10, ts(2026, 12, 2), marks={"ABC": D("105")})
    assert st["opening"]["account_value"] == 0 and st["deposits_total"] == D("10000")
    assert st["trade_count"] == 1 and st["fees"] == D("1") and st["bought"] == D("1000")
    assert st["closing"]["account_value"] == D("10049")
    assert st["positions"] == [{"symbol": "ABC", "description": "ABC", "qty": D("10"), "cost_basis": D("1000"),
                                "mark": D("105"), "market_value": D("1050"), "unrealized_pnl": D("50")}]
    assert st["reconciliation_difference"] == 0


def test_statement_current_month_uses_live_value_and_other_income():
    a = statement_account()
    a.ledger.post(ts(2026, 12, 1, 10), "dividend", "Dividend: ABC", [("cash", D("12.5")), ("income:dividends", D("-12.5"))])
    st = monthly_statement(a, 2026, 12, ts(2026, 12, 2), live_equity=D("10110.50"))
    assert st["current"] and st["closing"]["source"] == "live account value"
    assert st["other_by_kind"] == {"dividend": D("12.5")} and st["other_total"] == D("12.5")
    assert st["reconciliation_difference"] == 0


def test_statement_errors_and_html_escaping():
    a = statement_account()
    with pytest.raises(ValueError, match="not started"):
        monthly_statement(a, 2027, 1, ts(2026, 12, 2))
    with pytest.raises(ValueError, match="did not exist"):
        monthly_statement(a, 2026, 9, ts(2026, 12, 2))
    html = statement_html(monthly_statement(a, 2026, 11, ts(2026, 12, 2)))
    assert "Test &lt;b&gt;account&lt;/b&gt;" in html and "<b>account</b>" not in html
    assert "Paper trading: simulated" in html and "10,098.00" in html


def test_month_helpers():
    assert parse_month("2026-11") == (2026, 11)
    for bad in ("2026-13", "26-11", "november", "2026-1"):
        with pytest.raises(ValueError):
            parse_month(bad)
    assert months_between(ts(2026, 10, 15), ts(2027, 1, 2)) == ["2027-01", "2026-12", "2026-11", "2026-10"]
