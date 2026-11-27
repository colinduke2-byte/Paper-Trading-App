"""Tax lots: the open lots of every position, and the tax-year summary.

GET /api/accounts/{aid}/lots[?symbol=ABC]
    Open lots per position: id, open date, quantity, cost per unit, cost basis, current value
    (with the quote's source), unrealised gain, holding period and the date a long lot turns
    long-term, the quantity named by working orders, and any informational wash-sale
    adjustment the lot carries.
GET /api/accounts/{aid}/tax-summary[?year=2026]
    Short-term and long-term totals for one calendar year, with wash-sale adjustments.

To sell chosen lots, send "lots": [{"lot_id": "L3", "qty": 5}, ...] with a closing order to
POST /api/accounts/{aid}/orders (or its /preview route); the quantities must add up to the order's.
"""
from decimal import Decimal
from typing import Optional

from ..analytics import wash
from ..analytics.common import ny_date
from ..analytics.trades import closed_lots, tax_year_summary, tax_years
from ..core.money import ZERO, cents
from ..ledger.account import is_long_term, long_term_from
from ..options.symbology import describe, multiplier, parse as parse_option

FOUR = Decimal("0.0001")
LOTS_NOTE = ("Holding periods use New York dates: a lot held for more than one year is long-term. Short "
             "positions are always short-term. Wash-sale adjustments are informational only, not tax advice.")


def lot_rows(ex, account, symbol: Optional[str] = None) -> list:
    rows = closed_lots(account)
    _, open_adj = wash.analyse(account, rows)
    reserved = ex.reserved_lots(account.id)
    fill_kind = {f.id: f.kind for f in account.fills}
    now = ex.now()
    today = ny_date(now)
    out = []
    for sym in sorted(account.lots):
        lots = account.lots[sym]
        if symbol and sym != symbol or not lots:
            continue
        mult = multiplier(sym)
        mark = ex.mark(sym)
        items = []
        for lot in lots:
            q = abs(lot.qty)
            long = lot.qty > 0
            signed_cost = lot.cost if long else -lot.cost
            value = cents(lot.qty * mark * mult)
            lt_on = long_term_from(lot.ts) if long else None
            adj = wash.open_lot_adjustment(open_adj, lot)
            item = {"id": lot.id, "symbol": sym, "direction": "long" if long else "short",
                    "opened_at": lot.ts, "open_date": ny_date(lot.ts).isoformat(), "opened_by": fill_kind.get(lot.fill_id, "trade"),
                    "open_fill_id": lot.fill_id, "qty": lot.qty, "price": lot.price,
                    "cost_per_unit": (lot.cost / (q * mult)).quantize(FOUR) if q else ZERO,
                    "cost_basis": lot.cost, "market_value": value, "unrealized_pnl": value - signed_cost,
                    "term": "long" if is_long_term(lot, now) else "short",
                    "long_term_on": lt_on.isoformat() if lt_on else None,
                    "days_to_long_term": max((lt_on - today).days, 0) if lt_on else None,
                    "reserved": min(reserved.get(lot.id, ZERO), q), "available": max(q - reserved.get(lot.id, ZERO), ZERO),
                    "wash_sale": None}
            if adj:
                shift = adj["holding_shift"]
                adj_lt = long_term_from(lot.ts - shift) if long else None
                item["wash_sale"] = {
                    "qty": min(adj["qty"], q), "disallowed_loss_added": adj["amount"],
                    # long: the basis grows; short: the opening proceeds shrink. Either way the gain is smaller.
                    "adjusted_cost_basis": lot.cost + adj["amount"] if long else lot.cost - adj["amount"],
                    "adjusted_unrealized_pnl": item["unrealized_pnl"] - adj["amount"],
                    "adjusted_holding_start": ny_date(lot.ts - shift).isoformat(),
                    "adjusted_long_term_on": adj_lt.isoformat() if adj_lt else None,
                    "from_closed_lots": adj["from"],
                    "note": "Informational: a loss disallowed by a wash sale would be added to this lot's basis."}
            items.append(item)
        c = parse_option(sym)
        out.append({"symbol": sym, "description": describe(sym), "kind": "option" if c else "stock",
                    "underlying": c.underlying if c else sym, "multiplier": mult,
                    "qty": sum((l.qty for l in lots), ZERO), "mark": mark, "source": ex.quote_source(sym),
                    "lots": items})
    return out


def register(app, ctx):
    @app.get("/api/accounts/{aid}/lots")
    def lots(aid: str, symbol: Optional[str] = None):
        e = ctx.ex
        a = e.account(aid)
        sym = (symbol or "").upper().strip() or None
        return ctx.plain({"account_id": aid, "lot_method": a.lot_method, "positions": lot_rows(e, a, sym),
                          "notes": [LOTS_NOTE], "simulated": True})

    @app.get("/api/accounts/{aid}/tax-summary")
    def tax_summary(aid: str, year: Optional[int] = None):
        e = ctx.ex
        a = e.account(aid)
        years = tax_years(a)
        y = year if year is not None else (years[0] if years else ny_date(e.now()).year)
        return ctx.plain(dict(tax_year_summary(a, y), years=years))
