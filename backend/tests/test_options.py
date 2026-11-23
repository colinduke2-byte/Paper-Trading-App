import math
from datetime import date

import pytest

from app.options import chain, pricing
from app.options.symbology import build, describe, multiplier, parse
from helpers import D


def test_reference_prices():
    # Hull, Options, Futures and Other Derivatives: S=42, K=40, r=10%, sigma=20%, T=0.5
    assert pricing.price(42, 40, 0.5, 0.10, 0.20, "C") == pytest.approx(4.7594, abs=1e-4)
    assert pricing.price(42, 40, 0.5, 0.10, 0.20, "P") == pytest.approx(0.8086, abs=1e-4)
    # At the money, S=K=100, r=5%, sigma=20%, T=1
    assert pricing.price(100, 100, 1, 0.05, 0.20, "C") == pytest.approx(10.4506, abs=1e-4)
    assert pricing.price(100, 100, 1, 0.05, 0.20, "P") == pytest.approx(5.5735, abs=1e-4)


@pytest.mark.parametrize("S,K,T,r,v", [(100, 90, 0.25, 0.04, 0.3), (50, 60, 1.5, 0.02, 0.55), (310, 300, 0.02, 0.04, 0.2)])
def test_put_call_parity_and_greeks(S, K, T, r, v):
    c, p = pricing.price(S, K, T, r, v, "C"), pricing.price(S, K, T, r, v, "P")
    assert c - p == pytest.approx(S - K * math.exp(-r * T), abs=1e-9)
    for kind in "CP":
        g = pricing.greeks(S, K, T, r, v, kind)
        h = 1e-4
        f = lambda **kw: pricing.price(**{**dict(S=S, K=K, T=T, r=r, sigma=v, kind=kind), **kw})
        assert g["delta"] == pytest.approx((f(S=S + h) - f(S=S - h)) / (2 * h), abs=1e-5)
        assert g["gamma"] == pytest.approx((f(S=S + h) - 2 * f() + f(S=S - h)) / h ** 2, abs=1e-3)
        assert g["vega"] == pytest.approx((f(sigma=v + h) - f(sigma=v - h)) / (2 * h) / 100, abs=1e-5)
        assert g["theta"] == pytest.approx(-(f(T=T + h) - f(T=T - h)) / (2 * h) / 365, abs=1e-5)
        assert g["rho"] == pytest.approx((f(r=r + h) - f(r=r - h)) / (2 * h) / 100, abs=1e-5)


def test_implied_vol_round_trip():
    for S, K, T, v, kind in [(100, 100, 0.5, 0.25, "C"), (100, 130, 0.25, 0.6, "C"), (100, 75, 1.0, 0.4, "P"), (20, 22, 0.05, 1.2, "P")]:
        px = pricing.price(S, K, T, 0.04, v, kind)
        assert pricing.implied_vol(px, S, K, T, 0.04, kind) == pytest.approx(v, abs=1e-5)
    assert pricing.implied_vol(0.5, 100, 90, 0.5, 0.04, "C") is None  # below intrinsic value


def test_expired_option_is_intrinsic():
    assert pricing.price(105, 100, 0, 0.04, 0.3, "C") == 5
    assert pricing.price(105, 100, 0, 0.04, 0.3, "P") == 0


def test_symbology():
    sym = build("ABC", date(2026, 12, 18), "C", D("152.5"))
    assert sym == "ABC261218C00152500"
    c = parse(sym)
    assert (c.underlying, c.expiry, c.kind, c.strike) == ("ABC", date(2026, 12, 18), "C", D("152.5"))
    assert multiplier(sym) == 100 and multiplier("ABC") == 1
    assert parse("ABC") is None and parse("ABC261340C00100000") is None
    assert describe(sym) == "ABC 18 Dec 2026 152.5 Call"


def test_chain_shape():
    exps = chain.expirations(date(2026, 11, 23))
    assert exps[0] == date(2026, 11, 27) and date(2026, 12, 24) in exps  # Christmas Friday moves to Thursday
    assert exps == sorted(set(exps))
    ks = chain.strikes(182.4)
    assert D("180") in ks and all(k % 5 == 0 for k in ks)
    rows = chain.chain("ABC", exps[2], 100.0, 0.3, 1795500000.0)
    for r in rows:
        for side in ("call", "put"):
            o = r[side]
            assert 0 <= o["bid"] < o["ask"] and o["iv"] > 0
    atm = min(rows, key=lambda r: abs(float(r["strike"]) - 100))
    assert 0.4 < atm["call"]["delta"] < 0.65 and -0.6 < atm["put"]["delta"] < -0.35
