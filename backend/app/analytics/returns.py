"""Return and risk statistics over an account-value series.

Conventions
- A series is a list of (timestamp, value) pairs in time order.
- Flows are (timestamp, amount) pairs: deposits positive, withdrawals negative. A flow at time t with
  t_prev < t <= t_i is treated as arriving at the start of the sub-period that ends at t_i, so the
  sub-period return is V_i / (V_prev + flow) - 1. Flows at or before the first point are already in it.
- Time-weighted return chains the sub-period returns, so deposits and withdrawals do not count as gains.
- Daily returns use the last value of each New York calendar day. Volatility is the sample standard
  deviation of daily returns times sqrt(periods_per_year); Sharpe is the mean daily excess return over
  its standard deviation, times sqrt(periods_per_year).
"""
from bisect import bisect_right
from decimal import Decimal, localcontext
from typing import Optional

from ..core.money import ZERO
from .common import ny_date, ratio

ONE = Decimal(1)


def _flows_between(flows: list, lo: float, hi: float) -> Decimal:
    return sum((a for t, a in flows if lo < t <= hi), ZERO)


def period_returns(series: list, flows: list = ()) -> list:
    """[(ts, return)] for each step of the series, net of external flows."""
    out = []
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        base = v0 + _flows_between(flows, t0, t1)
        out.append((t1, v1 / base - ONE if base > 0 else ZERO))
    return out


def growth_index(series: list, flows: list = ()) -> list:
    """[(ts, index)] starting at 1: the value of one unit invested at the start."""
    if not series:
        return []
    idx, out = ONE, [(series[0][0], ONE)]
    for t, r in period_returns(series, flows):
        idx *= ONE + r
        out.append((t, idx))
    return out


def time_weighted_return(series: list, flows: list = ()) -> Optional[Decimal]:
    if len(series) < 2:
        return None
    return growth_index(series, flows)[-1][1] - ONE


def daily_closes(series: list) -> list:
    """Opening point plus the last point of each New York day: [(ts, value)]."""
    if not series:
        return []
    out = [series[0]]
    last_day = None
    for t, v in series[1:]:
        d = ny_date(t)
        if d == last_day:
            out[-1] = (t, v)
        else:
            out.append((t, v))
            last_day = d
    # The opening point and the first day's close may fall on the same day; keep both, which makes the
    # first daily return run from the opening value to that day's close.
    return out


def daily_returns(series: list, flows: list = ()) -> list:
    """[(date, return)] for each New York day with a value."""
    closes = daily_closes(series)
    return [(ny_date(t), r) for t, r in period_returns(closes, flows)]


def _mean(xs: list) -> Decimal:
    return sum(xs, ZERO) / len(xs)


def _stdev(xs: list) -> Optional[Decimal]:
    if len(xs) < 2:
        return None
    m = _mean(xs)
    with localcontext() as ctx:
        ctx.prec = 34
        return (sum(((x - m) ** 2 for x in xs), ZERO) / (len(xs) - 1)).sqrt()


def volatility(returns: list, periods_per_year: int = 252) -> Optional[Decimal]:
    sd = _stdev(returns)
    if sd is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 34
        return sd * Decimal(periods_per_year).sqrt()


def sharpe(returns: list, risk_free_annual: Decimal = ZERO, periods_per_year: int = 252) -> Optional[Decimal]:
    sd = _stdev(returns)
    if sd is None or sd == 0:
        return None
    excess = _mean(returns) - risk_free_annual / periods_per_year
    with localcontext() as ctx:
        ctx.prec = 34
        return excess / sd * Decimal(periods_per_year).sqrt()


def max_drawdown(index: list) -> dict:
    """Largest peak-to-trough fall of a growth index, with when it peaked, bottomed and recovered.

    Recovery is the first point after the trough that is back at or above the peak (None if not yet).
    """
    best = {"max_drawdown": ZERO, "peak_at": None, "trough_at": None, "recovered_at": None}
    if not index:
        return best
    peak_t, peak_v, best_peak_v, trough_i = index[0][0], index[0][1], None, None
    for i, (t, v) in enumerate(index):
        if v >= peak_v:
            peak_t, peak_v = t, v
            continue
        depth = (peak_v - v) / peak_v
        if depth > best["max_drawdown"]:
            best = {"max_drawdown": depth, "peak_at": peak_t, "trough_at": t, "recovered_at": None}
            best_peak_v, trough_i = peak_v, i
    if trough_i is not None:
        best["recovered_at"] = next((t for t, v in index[trough_i + 1:] if v >= best_peak_v), None)
    return best


def price_at(history: list, ts: float) -> Optional[Decimal]:
    """Last price at or before ts from [(ts, price)] in time order; the first price if ts is earlier."""
    if not history:
        return None
    i = bisect_right([h[0] for h in history], ts)
    return history[i - 1][1] if i else history[0][1]


def benchmark_series(series_ts: list, history: list) -> list:
    """[(ts, price)] of the benchmark at each account timestamp."""
    times = [h[0] for h in history]
    out = []
    for t in series_ts:
        i = bisect_right(times, t)
        out.append((t, history[i - 1][1] if i else history[0][1]))
    return out


def _beta(a: list, b: list) -> tuple:
    if len(a) < 2:
        return None, None
    ma, mb = _mean(a), _mean(b)
    cov = sum(((x - ma) * (y - mb) for x, y in zip(a, b)), ZERO) / (len(a) - 1)
    va, vb = _stdev(a), _stdev(b)
    beta = cov / (vb * vb) if vb else None
    corr = cov / (va * vb) if va and vb else None
    return beta, corr


def report(series: list, flows: list = (), benchmark_history: Optional[list] = None,
           risk_free_annual: Decimal = ZERO, periods_per_year: int = 252) -> dict:
    """Everything the performance view needs, as Decimals (ratios to 10 places)."""
    idx = growth_index(series, flows)
    daily = daily_returns(series, flows)
    rets = [r for _, r in daily]
    dd = max_drawdown(idx)
    out = {
        "start": series[0][0] if series else None, "end": series[-1][0] if series else None,
        "start_value": series[0][1] if series else None, "end_value": series[-1][1] if series else None,
        "net_deposits": sum((a for t, a in flows if series and series[0][0] < t <= series[-1][0]), ZERO),
        "time_weighted_return": ratio(time_weighted_return(series, flows)),
        "daily_returns": [{"date": d.isoformat(), "return": ratio(r)} for d, r in daily],
        "trading_days": len(daily),
        "volatility": ratio(volatility(rets, periods_per_year)),
        "sharpe": ratio(sharpe(rets, risk_free_annual, periods_per_year)),
        "risk_free_rate": risk_free_annual,
        "max_drawdown": ratio(dd["max_drawdown"]), "drawdown_peak_at": dd["peak_at"],
        "drawdown_trough_at": dd["trough_at"], "drawdown_recovered_at": dd["recovered_at"],
        "index": idx, "benchmark": None,
    }
    if benchmark_history and series:
        bench = benchmark_series([t for t, _ in series], benchmark_history)
        p0 = bench[0][1]
        b_idx = [(t, p / p0) for t, p in bench] if p0 else []
        b_daily = daily_returns(bench)
        if b_idx:
            b_ret = b_idx[-1][1] - ONE
            beta, corr = _beta(rets, [r for _, r in b_daily]) if len(b_daily) == len(daily) else (None, None)
            twr = out["time_weighted_return"]
            out["benchmark"] = {
                "start_price": p0, "end_price": bench[-1][1], "total_return": ratio(b_ret),
                "excess_return": ratio(twr - b_ret) if twr is not None else None,
                "max_drawdown": ratio(max_drawdown(b_idx)["max_drawdown"]),
                "volatility": ratio(volatility([r for _, r in b_daily], periods_per_year)),
                "beta": ratio(beta), "correlation": ratio(corr), "index": b_idx,
            }
    return out
