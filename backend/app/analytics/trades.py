"""Closed-lot trade statistics and the realised gain/loss report.

A "trade" here is one closed tax lot: each time a fill closes some or all of an open lot, the account
records one closed entry. Realised gains exclude fees (fees are their own ledger account), so the sum
of every row's gain equals the ledger's realised balance exactly.
"""
from decimal import Decimal
from typing import Optional

from ..core.money import ZERO, D, cents
from ..options.symbology import describe, multiplier, parse as parse_option
from .common import anniversary, iso, ny_date, ratio

WASH_WINDOW_DAYS = 30
WASH_NOTE = ("The wash-sale flag is informational only. It marks a loss closed within 30 days before or "
             "after another purchase of the same symbol (another short sale, for a short lot). It does not "
             "check substantially identical securities, adjust cost basis or defer any loss, and this "
             "report is not tax advice.")
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
            "asset": "option" if parse_option(c["symbol"]) else "stock",
            "opened_at": float(c["opened_at"]), "closed_at": float(c["closed_at"]),
            "open_price": D(c["open_price"]), "close_price": D(c["close_price"]),
            "proceeds": proceeds, "cost": cost, "gain": gain,
            "open_fill_id": c.get("open_fill_id", ""), "close_fill_id": c.get("close_fill_id", ""),
            "order_id": c.get("order_id"), "kind": c.get("kind", "trade"), "lot_method": c.get("lot_method", "fifo"),
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


def _acquisitions(account, rows: list) -> list:
    """Every lot opening as (symbol, direction, ts, key), counted once per opening fill."""
    seen, out = set(), []

    def add(symbol, direction, ts, fill_id):
        key = fill_id or f"ts:{symbol}:{direction}:{ts}"
        if key not in seen:
            seen.add(key)
            out.append((symbol, direction, float(ts), key))
    for r in rows:
        add(r["symbol"], r["direction"], r["opened_at"], r["open_fill_id"])
    for sym, lots in account.lots.items():
        for lot in lots:
            add(sym, "long" if lot.qty > 0 else "short", lot.ts, getattr(lot, "fill_id", ""))
    return out


def wash_sale_matches(row: dict, acquisitions: list) -> list:
    """Openings of the same symbol and direction within 30 days of a losing close, other than its own."""
    if row["gain"] >= 0:
        return []
    own = row["open_fill_id"] or f"ts:{row['symbol']}:{row['direction']}:{row['opened_at']}"
    close_day = ny_date(row["closed_at"])
    return [a for a in acquisitions
            if a[0] == row["symbol"] and a[1] == row["direction"] and a[3] != own
            and abs((ny_date(a[2]) - close_day).days) <= WASH_WINDOW_DAYS]


def gains_report(account, start: Optional[float] = None, end: Optional[float] = None) -> dict:
    """Realised gain/loss by lot, optionally limited to lots closed in [start, end)."""
    rows = closed_lots(account)
    acq = _acquisitions(account, rows)
    out = []
    for r in rows:
        if (start is not None and r["closed_at"] < start) or (end is not None and r["closed_at"] >= end):
            continue
        matches = wash_sale_matches(r, acq)
        out.append(dict(r, open_date=ny_date(r["opened_at"]).isoformat(), close_date=ny_date(r["closed_at"]).isoformat(),
                        opened_iso=iso(r["opened_at"]), closed_iso=iso(r["closed_at"]),
                        term=term(r), wash_sale=bool(matches),
                        wash_sale_with=[{"opened_at": a[2], "fill_id": "" if a[3].startswith("ts:") else a[3]} for a in matches]))
    total = lambda f: sum((r["gain"] for r in out if f(r)), ZERO)
    return {
        "lot_method": account.lot_method, "rows": out,
        "totals": {"gain": total(lambda r: True), "short_term": total(lambda r: r["term"] == "short"),
                   "long_term": total(lambda r: r["term"] == "long"),
                   "proceeds": sum((r["proceeds"] for r in out), ZERO), "cost": sum((r["cost"] for r in out), ZERO),
                   "wash_sale_lots": sum(1 for r in out if r["wash_sale"]),
                   "wash_sale_losses": total(lambda r: r["wash_sale"])},
        "notes": [TERM_NOTE, WASH_NOTE, "Gains exclude commissions and fees, which are reported separately."],
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
                          "asset": "option" if c else "stock", "market_value": mv, "weight": pct(mv)})
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
