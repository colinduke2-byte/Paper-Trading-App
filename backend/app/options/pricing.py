"""Black-Scholes pricing, greeks and implied volatility (European exercise).

Model code works in floats; results are converted to Decimal prices at the edge.
"""
import math

SQRT_2PI = math.sqrt(2 * math.pi)


def _cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def intrinsic(S: float, K: float, kind: str) -> float:
    return max(S - K, 0.0) if kind == "C" else max(K - S, 0.0)


def price(S: float, K: float, T: float, r: float, sigma: float, kind: str) -> float:
    if T <= 0 or sigma <= 0:
        return intrinsic(S, K, kind)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
    d2 = d1 - sq
    if kind == "C":
        return S * _cdf(d1) - K * math.exp(-r * T) * _cdf(d2)
    return K * math.exp(-r * T) * _cdf(-d2) - S * _cdf(-d1)


def greeks(S: float, K: float, T: float, r: float, sigma: float, kind: str) -> dict:
    """delta, gamma, theta (per calendar day), vega (per 1 vol point), rho (per 1 rate point)."""
    if T <= 0 or sigma <= 0:
        itm = intrinsic(S, K, kind) > 0
        return {"delta": (1.0 if kind == "C" else -1.0) if itm else 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
    d2 = d1 - sq
    disc = math.exp(-r * T)
    gamma = _pdf(d1) / (S * sq)
    vega = S * _pdf(d1) * math.sqrt(T)
    common = -S * _pdf(d1) * sigma / (2 * math.sqrt(T))
    if kind == "C":
        delta, theta, rho = _cdf(d1), common - r * K * disc * _cdf(d2), K * T * disc * _cdf(d2)
    else:
        delta, theta, rho = _cdf(d1) - 1, common + r * K * disc * _cdf(-d2), -K * T * disc * _cdf(-d2)
    return {"delta": delta, "gamma": gamma, "theta": theta / 365, "vega": vega / 100, "rho": rho / 100}


def implied_vol(target: float, S: float, K: float, T: float, r: float, kind: str):
    """Bisection solver. Returns None when the price is outside the no-arbitrage range."""
    if T <= 0:
        return None
    lo, hi = 1e-4, 5.0
    if target < price(S, K, T, r, lo, kind) - 1e-9 or target > price(S, K, T, r, hi, kind) + 1e-9:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if price(S, K, T, r, mid, kind) < target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-8:
            break
    return 0.5 * (lo + hi)
