"""Closed-lot trade statistics and the realised gain/loss report.

A "trade" here is one closed tax lot: each time a fill closes some or all of an open lot, the account
records one closed entry. Realised gains exclude fees (fees are their own ledger account), so the sum
of every row's gain equals the ledger's realised balance exactly.
"""
from decimal import Decimal
from typing import Optional

from ..core.assets import asset_class
from ..core.money import ZERO, D, cents
from ..options.symbology import describe, multiplier, parse as parse_option
from . import wash
from .common import anniversary, iso, month_bounds, ny_date, ratio

WASH_WINDOW_DAYS = wash.WINDOW_DAYS
WASH_NOTE = wash.NOTE
TERM_NOTE = ("Long-term means the lot was held for more than one year (sold after the one-year anniversary "
             "of the purchase date, New York time). Short sales are shown as short-term.")


def closed_lots(account) -> list:
    """Every closed lot as a normalised record with Decimal money."""
    out = []
    for i, c in enumerate(account.closed):
        signed_qty = D(c["qty"])
        long = signed_qty > 0
        q = abs(signed_qty)
        gain = D(c["pnl"])
        if "close_amount" in c:
            close_amount, basis = D(c["close_amount"]), D(c["basis"])
        else:  # entries saved before the amounts were recorded
            close_amount = cents(q * D(c["close_price"]) * multiplier(c["symbol"]))
            basis = close_amount - gain if long else close_amount + gain
        # Long lot: proceeds are the closing sale, cost the purchase. Short lot: proceeds are the
        # opening sale, cost the closing purchase. Either way gain = proceeds - cost.
        proceeds, cost = (close_amount, basis) if long else (basis, close_amount)
        out.append({
            "id": c.get("id", f"C{i + 1}"), "symbol": c["symbol"], "description": describe(c["symbol"]),
            "qty": q, "direction": "long" if long else "short",
            "asset": asset_class(c["symbol"]),
            "opened_at": float(c["opened_at"]), "closed_at": float(c["closed_at"]),
            "open_price": D(c["open_price"]), "close_price": D(c["close_price"]),
            "proceeds": proceeds, "cost": cost, "gain": gain,
            "open_fill_id": c.get("open_fill_id", ""), "close_fill_id": c.get("close_fill_id", ""),
            "order_id": c.get("order_id"), "kind": c.get("kind", "trade"), "lot_method": c.get("lot_method", "fifo"),
            "lot_id": c.get("lot_id", ""), "lot_fallback": bool(c.get("lot_fallback")),
        })
    return out


def _stats(rows: list) -> dict:
    gains = [r["gain"] for r in rows]
    wins = [g for g in gains if g > 0]
    losses = [g for g in gains if g < 0]
    gross_win, gross_loss = sum(wins, ZERO), -sum(losses, ZERO)
    n = len(gains)
    held = [r["closed_at"] - r["opened_at"] for r in rows]
    return {
        "trades": n, "wins": len(wins), "losses": len(losses), "scratches": n - len(wins) - len(losses),
        "win_rate": ratio(Decimal(len(wins)) / n) if n else None,
        "total_gain": sum(gains, ZERO), "gross_win": gross_win, "gross_loss": gross_loss,
        "average_win": cents(gross_win / len(wins)) if wins else None,
        "average_loss": cents(-gross_loss / len(losses)) if losses else None,
        "profit_factor": ratio(gross_win / gross_loss) if gross_loss else None,
        "expectancy": cents(sum(gains, ZERO) / n) if n else None,
        "largest_win": max(wins) if wins else None, "largest_loss": min(losses) if losses else None,
        "average_holding_seconds": sum(held) / n if n else None,
    }


def trade_stats(rows: list) -> dict:
    """Overall statistics plus breakdowns by symbol, by long/short and by stock/option."""
    def group(key):
        keys = sorted({r[key] for r in rows})
        return {k: _stats([r for r in rows if r[key] == k]) for k in keys}
    return {"overall": _stats(rows), "by_symbol": group("symbol"), "by_direction": group("direction"),
            "by_asset": group("asset")}


def term(row: dict) -> str:
    if row["direction"] == "short":
        return "short"
    return "long" if ny_date(row["closed_at"]) > anniversary(ny_date(row["opened_at"])) else "short"


def gains_report(account, start: Optional[float] = None, end: Optional[float] = None) -> dict:
    """Realised gain/loss by lot, optionally limited to lots closed in [start, end).

    `gain` is the ledger's realised amount and always sums to the ledger's realised balance.
    The wash-sale fields (see wash.py) are informational: `wash_sale_disallowed` is the part of a
    loss disallowed because of replacement shares, `basis_adjustment` the disallowed loss of an
    earlier sale carried into this lot, and `adjusted_gain` = gain - basis_adjustment + disallowed.
    """
    rows = closed_lots(account)
    detail, _ = wash.analyse(account, rows)  # over every lot: the 30-day window crosses any range
    out = []
    for r, w in zip(rows, detail):
        if (start is not None and r["closed_at"] < start) or (end is not None and r["closed_at"] >= end):
            continue
        reps = [dict(x, fill_id=x["fill_id"] or "") for x in w["replacements"]]
        out.append(dict(r, open_date=ny_date(r["opened_at"]).isoformat(), close_date=ny_date(r["closed_at"]).isoformat(),
                        opened_iso=iso(r["opened_at"]), closed_iso=iso(r["closed_at"]),
                        term=term(r), wash_sale=w["disallowed"] > 0, wash_sale_with=reps,
                        wash_sale_disallowed=w["disallowed"], loss_before_wash=w["loss_before_wash"],
                        basis_adjustment=w["basis_adjustment"], adjusted_cost=w["adjusted_cost"],
                        adjusted_gain=w["adjusted_gain"], adjusted_open_date=ny_date(w["adjusted_opened_at"]).isoformat(),
                        adjusted_term=w["adjusted_term"], basis_carried_from=w["carried_from"]))
    total = lambda f, k="gain": sum((r[k] for r in out if f(r)), ZERO)
    return {
        "lot_method": account.lot_method, "rows": out,
        "totals": {"gain": total(lambda r: True), "short_term": total(lambda r: r["term"] == "short"),
                   "long_term": total(lambda r: r["term"] == "long"),
                   "proceeds": sum((r["proceeds"] for r in out), ZERO), "cost": sum((r["cost"] for r in out), ZERO),
                   "wash_sale_lots": sum(1 for r in out if r["wash_sale"]),
                   "wash_sale_losses": total(lambda r: r["wash_sale"]),
                   "wash_sale_disallowed": total(lambda r: True, "wash_sale_disallowed"),
                   "basis_adjustments": total(lambda r: True, "basis_adjustment"),
                   "adjusted_gain": total(lambda r: True, "adjusted_gain"),
                   "adjusted_short_term": total(lambda r: r["adjusted_term"] == "short", "adjusted_gain"),
                   "adjusted_long_term": total(lambda r: r["adjusted_term"] == "long", "adjusted_gain")},
        "notes": [TERM_NOTE, WASH_NOTE, "Gains exclude commissions and fees, which are reported separately."],
    }


def _bucket(rows: list, gain_key: str = "gain") -> dict:
    return {"lots": len(rows), "proceeds": sum((r["proceeds"] for r in rows), ZERO),
            "cost": sum((r["cost"] for r in rows), ZERO), "gain": sum((r["gain"] for r in rows), ZERO),
            "wash_sale_disallowed": sum((r["wash_sale_disallowed"] for r in rows), ZERO),
            "basis_adjustments": sum((r["basis_adjustment"] for r in rows), ZERO),
            "adjusted_gain": sum((r["adjusted_gain"] for r in rows), ZERO)}


def tax_years(account) -> list:
    """Calendar years (New York time) in which the account closed any lot, newest first."""
    return sorted({ny_date(float(c["closed_at"])).year for c in account.closed}, reverse=True)


def tax_year_summary(account, year: int) -> dict:
    """Short-term and long-term totals for lots closed in one calendar year, with wash-sale adjustments.

    `gain` columns are the ledger's realised amounts. `adjusted_gain` columns apply the informational
    wash-sale adjustments, grouped by the adjusted holding period.
    """
    if not 1970 <= year <= 9999:
        raise ValueError("year must be written as YYYY, for example 2026")
    start, end = month_bounds(year, 1)[0], month_bounds(year, 12)[1]
    rep = gains_report(account, start, end)
    rows = rep["rows"]
    short = [r for r in rows if r["term"] == "short"]
    long = [r for r in rows if r["term"] == "long"]
    return {
        "year": year, "lot_method": account.lot_method,
        "short_term": _bucket(short), "long_term": _bucket(long), "total": _bucket(rows),
        "adjusted": {"short_term": sum((r["adjusted_gain"] for r in rows if r["adjusted_term"] == "short"), ZERO),
                     "long_term": sum((r["adjusted_gain"] for r in rows if r["adjusted_term"] == "long"), ZERO),
                     "total": sum((r["adjusted_gain"] for r in rows), ZERO)},
        "wash_sales": [{"id": r["id"], "lot_id": r.get("lot_id", ""), "symbol": r["symbol"], "description": r["description"],
                        "close_date": r["close_date"], "qty": r["qty"], "loss": r["loss_before_wash"],
                        "disallowed": r["wash_sale_disallowed"], "replacements": r["wash_sale_with"]}
                       for r in rows if r["wash_sale"]],
        "by_method": {m: _bucket([r for r in rows if r["lot_method"] == m]) for m in sorted({r["lot_method"] for r in rows})},
        "notes": rep["notes"] + ["Informational only, not tax advice. Check figures against your broker's tax forms."],
    }


def exposure(rows: list, sectors: dict, equity: Decimal) -> dict:
    """Market value by symbol and sector. rows: dicts with symbol, qty and market_value (Decimal).

    Options count at market value (premium) under their underlying's sector, not delta-adjusted.
    """
    def pct(v):
        return ratio(v / equity) if equity else None
    by_symbol, by_sector, unpriced = [], {}, []
    for r in rows:
        if r.get("market_value") is None:  # no price yet: left out rather than counted at zero
            unpriced.append(r["symbol"])
            continue
        mv = D(r["market_value"])
        c = parse_option(r["symbol"])
        under = c.underlying if c else r["symbol"]
        sector = sectors.get(under, "Other")
        by_symbol.append({"symbol": r["symbol"], "description": describe(r["symbol"]), "sector": sector,
                          "asset": asset_class(r["symbol"]), "market_value": mv, "weight": pct(mv)})
        s = by_sector.setdefault(sector, {"sector": sector, "long": ZERO, "short": ZERO})
        s["long" if mv > 0 else "short"] += mv
    sectors_out = []
    for s in sorted(by_sector.values(), key=lambda s: -(s["long"] - s["short"])):
        s.update(net=s["long"] + s["short"], gross=s["long"] - s["short"])
        s.update(net_weight=pct(s["net"]), gross_weight=pct(s["gross"]))
        sectors_out.append(s)
    long = sum((x["market_value"] for x in by_symbol if x["market_value"] > 0), ZERO)
    short = sum((x["market_value"] for x in by_symbol if x["market_value"] < 0), ZERO)
    by_symbol.sort(key=lambda x: -abs(x["market_value"]))
    return {"by_symbol": by_symbol, "by_sector": sectors_out, "unpriced": unpriced,
            "totals": {"long": long, "short": short, "net": long + short, "gross": long - short,
                       "equity": equity, "gross_leverage": pct(long - short), "net_leverage": pct(long + short)},
            "note": "Options count at market value under their underlying's sector; exposure is not delta-adjusted."}
