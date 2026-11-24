"""Indicators from daily bars. Chart data is float by design, so these are floats (display only)."""
import math
from datetime import date, timedelta
from typing import Optional

PERIODS = {"1D": 1, "1W": 7, "1M": 30, "3M": 91, "1Y": 365}


def bar_date(t: float) -> date:
    """Daily bars are stamped at UTC midnight of their trading date (both feeds)."""
    return date(1970, 1, 1) + timedelta(days=int(t // 86400))


def split_today(bars: list, today: date) -> tuple:
    """(completed daily bars, today's bar or None)."""
    if bars and bar_date(bars[-1]["time"]) >= today:
        return bars[:-1], bars[-1]
    return list(bars), None


def sma(values: list, n: int) -> Optional[float]:
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(closes: list, n: int = 14) -> Optional[float]:
    """Wilder's RSI over all the closes given (needs at least n + 1)."""
    if len(closes) < n + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100 - 100 / (1 + ag / al)


def pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or not b:
        return None
    return (a - b) / b * 100


def history_stats(history: list, last: Optional[float], today_bar: Optional[dict] = None,
                  volume: Optional[float] = None) -> dict:
    """Fields derived from completed daily bars plus the current price.

    history: completed daily bars, oldest first. last: the current price (None: the last close).
    The 52-week range covers the last 252 sessions and today's range when known.
    """
    closes = [float(b["close"]) for b in history]
    if last is None and closes:
        last = closes[-1]
    out = {"history_days": len(history)}
    if not closes or last is None:
        return out
    series = closes + [last]
    year = history[-252:]
    highs = [float(b["high"]) for b in year] + [last]
    lows = [float(b["low"]) for b in year] + [last]
    if today_bar:
        highs.append(float(today_bar["high"]))
        lows.append(float(today_bar["low"]))
    hi, lo = max(highs), min(lows)
    out.update(high_52w=hi, low_52w=lo,
               range_52w_pos=(last - lo) / (hi - lo) * 100 if hi > lo else 50.0,
               pct_from_high_52w=(hi - last) / hi * 100 if hi else None,
               pct_from_low_52w=(last - lo) / lo * 100 if lo else None)
    for n in (20, 50, 200):
        m = sma(series, n)
        out[f"sma{n}"] = m
        out[f"pct_vs_sma{n}"] = pct(last, m)
    out["rsi14"] = rsi(series[-120:], 14)
    vols = [float(b["volume"]) for b in history[-30:]]
    avg = sum(vols) / len(vols) if vols else None
    out["avg_volume_30d"] = avg
    out["rel_volume"] = volume / avg if avg and volume is not None else None
    rets = [math.log(series[i] / series[i - 1]) for i in range(max(1, len(series) - 252), len(series))
            if series[i - 1] > 0 and series[i] > 0]
    if len(rets) >= 20:
        mean = sum(rets) / len(rets)
        out["volatility"] = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) * 252)
    else:
        out["volatility"] = None
    return out


def performance(history: list, last: Optional[float], today: date, prev_close: Optional[float] = None) -> dict:
    """Price change in percent over each period: last versus the close on or before (today - days).

    1D is versus the previous close. A period longer than the history available is None.
    """
    out = {}
    if last is None:
        return {k: None for k in PERIODS}
    for key, days in PERIODS.items():
        if key == "1D":
            ref = prev_close if prev_close else (float(history[-1]["close"]) if history else None)
            out[key] = pct(last, ref)
            continue
        cutoff = today - timedelta(days=days)
        ref = None
        for b in reversed(history):
            if bar_date(b["time"]) <= cutoff:
                ref = float(b["close"])
                break
        out[key] = pct(last, ref)
    return out


def sparkline(bars: list, points: int = 40) -> list:
    """Closes reduced to at most `points` values (the last value is always kept)."""
    closes = [round(float(b["close"]), 4) for b in bars]
    if len(closes) <= points:
        return closes
    step = len(closes) / points
    out = [closes[min(int(i * step), len(closes) - 1)] for i in range(points - 1)]
    return out + [closes[-1]]
