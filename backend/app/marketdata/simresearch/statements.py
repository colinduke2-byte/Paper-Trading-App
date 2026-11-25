"""Simulated financial statements for one fictional company.

The model works in floats (it is a model), then every statement line is rounded to whole dollars as a
Decimal and the totals are built from the rounded lines, so the identities hold exactly:

- balance sheet: total assets = total liabilities + equity
- cash flow: operating + investing + financing = change in cash = cash at end - cash at start
- per share: EPS = net income / shares, rounded to the cent (so EPS x shares = net income within
  half a cent per share)

The four quarters ending at ANCHOR_QUARTER add up to the listed EPS times the listed share count. Per
share figures use the current share count, so they are adjusted for splits.
"""
import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from ...core.money import CENT, ZERO
from .profiles import ANCHOR_QUARTER, ESTIMATE_BIAS, NOISE_EPS, NOISE_EST, TAX_RATE, Profile, quarter_end, \
    quarter_label, rng

ONE = Decimal(1)
INTEREST_RATE = 0.045


def dollars(x: float) -> Decimal:
    return Decimal(repr(round(x)))


def per_share(total: Decimal, shares: Decimal) -> Decimal:
    return (total / shares).quantize(CENT, ROUND_HALF_UP) if shares else ZERO


class CompanyModel:
    """Quarterly history for one company. Results are cached per quarter index."""

    def __init__(self, seed: int, p: Profile, listed_eps: float, listed_shares: float, annual_dividends: float):
        self.seed, self.p = seed, p
        eps = listed_eps if abs(listed_eps) >= 0.01 else 0.01
        self.ni0 = eps * listed_shares
        self.r0 = self.ni0 / p.net_margin
        self.annual_dividends = max(annual_dividends, 0.0)  # total cash a year (does not change on a split)
        self.listed_shares = listed_shares
        mean_season = sum(p.season) / 4
        self._season = [s / mean_season for s in p.season]
        self._raw: dict = {}
        anchor = [self._raw_q(k) for k in range(ANCHOR_QUARTER - 3, ANCHOR_QUARTER + 1)]
        self.scale = self.ni0 / sum(a["ni"] for a in anchor)
        self._lines: dict = {}

    # ---- the float model ---------------------------------------------------
    def _raw_q(self, k: int) -> dict:
        hit = self._raw.get(k)
        if hit is not None:
            return hit
        p = self.p
        r = rng(self.seed, p.symbol, "quarter", k)
        t = (k - ANCHOR_QUARTER) / 4
        eps_noise, rev_noise = r.gauss(0, NOISE_EPS), r.gauss(0, 0.025)
        est_err, rev_est_err = r.gauss(-ESTIMATE_BIAS, NOISE_EST), r.gauss(-0.004, 0.01)
        cash_noise = r.gauss(0, 0.08)
        trend_rev = self.r0 / 4 * math.exp(p.growth * t) * self._season[k % 4]
        margin = p.net_margin * (math.exp(-0.12 * t) if p.net_margin < 0 else 1.0)  # losses narrow over time
        rev = trend_rev * math.exp(rev_noise)
        out = {"rev": rev, "ni": rev * margin * math.exp(eps_noise), "eps_noise": eps_noise, "est_err": est_err,
               "rev_est": rev * math.exp(-rev_noise + rev_est_err), "cash_noise": cash_noise}
        self._raw[k] = out
        return out

    def surprise_z(self, k: int) -> float:
        """Standardised profit surprise: positive is a beat (also for a loss-making company)."""
        a = self._raw_q(k)
        sign = 1.0 if self.p.net_margin > 0 else -1.0
        return sign * (a["eps_noise"] - a["est_err"]) / math.hypot(NOISE_EPS, NOISE_EST)

    # ---- Decimal statements ---------------------------------------------------
    def _balance(self, k: int) -> dict:
        p = self.p
        ttm_rev = sum(self._raw_q(j)["rev"] for j in range(k - 3, k + 1))
        assets = ttm_rev / p.asset_turnover
        equity = assets / (1 + p.debt_equity + p.other_liab)
        cash = dollars(assets * p.cash_frac * math.exp(self._raw_q(k)["cash_noise"]))
        ar, inv, ppe = dollars(assets * p.ar_frac), dollars(assets * p.inv_frac), dollars(assets * p.ppe_frac)
        other_assets = dollars(assets) - cash - ar - inv - ppe
        total_assets = cash + ar + inv + ppe + other_assets
        eq, debt = dollars(equity), dollars(equity * p.debt_equity)
        room = total_assets - eq - debt
        ap = min(dollars(assets * p.ap_frac), (room / 2).quantize(ONE))
        other_liab = room - ap
        total_liab = debt + ap + other_liab
        return {"cash": cash, "receivables": ar, "inventory": inv, "property_equipment": ppe,
                "other_assets": other_assets, "total_assets": total_assets, "accounts_payable": ap, "debt": debt,
                "other_liabilities": other_liab, "total_liabilities": total_liab, "equity": eq}

    def quarter(self, k: int, shares: Decimal) -> dict:
        """Income statement, balance sheet and cash flow for fiscal quarter k (calendar quarters)."""
        key = (k, shares)
        hit = self._lines.get(key)
        if hit is not None:
            return hit
        p, a = self.p, self._raw_q(k)
        bs, prev = self._balance(k), self._balance(k - 1)
        revenue, ni = dollars(a["rev"]), dollars(a["ni"] * self.scale)
        interest = dollars(float(prev["debt"]) * INTEREST_RATE / 4)
        pretax = dollars(float(ni) / (1 - TAX_RATE)) if ni > 0 else ni
        tax = pretax - ni
        operating = pretax + interest
        gross = max(dollars(a["rev"] * p.gross_margin), operating + dollars(a["rev"] * 0.05))
        income = {"revenue": revenue, "cost_of_revenue": revenue - gross, "gross_profit": gross,
                  "operating_expenses": gross - operating, "operating_income": operating, "interest_expense": interest,
                  "pretax_income": pretax, "income_tax": tax, "net_income": ni,
                  "shares": shares, "eps": per_share(ni, shares)}
        da = dollars(float(prev["property_equipment"]) * 0.03)
        wc = -(bs["receivables"] - prev["receivables"]) - (bs["inventory"] - prev["inventory"]) + \
            (bs["accounts_payable"] - prev["accounts_payable"])
        operating_cf = ni + da + wc
        capex = -dollars(a["rev"] * p.capex)
        dividends = -dollars(self.annual_dividends / 4) if self.annual_dividends else ZERO
        debt_change = bs["debt"] - prev["debt"]
        change = bs["cash"] - prev["cash"]
        other_fin = change - operating_cf - capex - dividends - debt_change
        financing = dividends + debt_change + other_fin
        cash_flow = {"net_income": ni, "depreciation": da, "working_capital": wc, "operating": operating_cf,
                     "capital_expenditure": capex, "investing": capex, "dividends_paid": dividends,
                     "debt_issued_repaid": debt_change, "buybacks_and_other": other_fin, "financing": financing,
                     "net_change_in_cash": operating_cf + capex + financing, "cash_start": prev["cash"],
                     "cash_end": bs["cash"], "free_cash_flow": operating_cf + capex}
        out = {"period": quarter_label(k), "quarter_index": k, "period_end": quarter_end(k).isoformat(),
               "income": income, "balance": bs, "cash_flow": cash_flow}
        self._lines[key] = out
        return out

    def year(self, y: int, shares: Decimal) -> dict:
        qs = [self.quarter(4 * y + i, shares) for i in range(4)]
        income = {k: sum((q["income"][k] for q in qs), ZERO) for k in qs[0]["income"] if k not in ("shares", "eps")}
        income["shares"] = shares
        income["eps"] = per_share(income["net_income"], shares)
        flows = {k: sum((q["cash_flow"][k] for q in qs), ZERO) for k in qs[0]["cash_flow"]}
        flows["cash_start"], flows["cash_end"] = qs[0]["cash_flow"]["cash_start"], qs[-1]["cash_flow"]["cash_end"]
        return {"period": f"FY {y}", "year": y, "period_end": quarter_end(4 * y + 3).isoformat(),
                "income": income, "balance": qs[-1]["balance"], "cash_flow": flows}

    def estimate(self, k: int, shares: Decimal) -> dict:
        """Consensus estimates published before the report: EPS and revenue."""
        a = self._raw_q(k)
        est_ni = a["ni"] * self.scale * math.exp(-a["eps_noise"] + a["est_err"])
        return {"eps_estimate": per_share(dollars(est_ni), shares), "revenue_estimate": dollars(a["rev_est"])}

    def ttm(self, last_k: int, shares: Decimal) -> Optional[dict]:
        qs = [self.quarter(k, shares) for k in range(last_k - 3, last_k + 1)]
        sums = lambda part, key: sum((q[part][key] for q in qs), ZERO)
        return {"revenue": sums("income", "revenue"), "gross_profit": sums("income", "gross_profit"),
                "operating_income": sums("income", "operating_income"), "net_income": sums("income", "net_income"),
                "operating_cf": sums("cash_flow", "operating"), "free_cash_flow": sums("cash_flow", "free_cash_flow"),
                "dividends": -sums("cash_flow", "dividends_paid"), "balance": qs[-1]["balance"]}
