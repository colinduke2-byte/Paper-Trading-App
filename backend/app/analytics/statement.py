"""Monthly account statement (JSON-ready dict and a printable HTML page).

Balances come from replaying the ledger up to each boundary, so cash, cost basis, realised gains and
fees are exact. Account value at a boundary is the saved account-value sample at or just before it
(the live value for the current month); unrealised profit is that value minus cash and cost basis, so
the statement always reconciles:
    closing value = opening value + deposits + realised - fees + other income + change in unrealised.
"""
from decimal import Decimal
from html import escape
from typing import Optional

from ..core.money import ZERO, D, cents
from ..ledger.ledger import CAPITAL, CASH, FEES, REALIZED
from ..options.symbology import describe, multiplier
from .common import iso, month_bounds, ny_date

TRADE_KINDS = ("trade", "exercise", "assignment", "expiration")


def balances_before(ledger, ts: float) -> dict:
    out: dict = {}
    for t in ledger.transactions:
        if t.ts < ts:
            for a, v in t.postings:
                out[a] = out.get(a, ZERO) + v
    return out


def _cost(bal: dict) -> Decimal:
    return sum((v for a, v in bal.items() if a.startswith("pos:")), ZERO)


def equity_at(account, ts: float, bal: dict) -> tuple:
    """(value, source) at ts. Before the account existed the value is zero."""
    if ts <= account.created_at:
        return ZERO, "before account opened"
    best = None
    for t, e in account.equity_history:
        if float(t) <= ts:
            best = D(e)
        else:
            break
    if best is None:
        return bal.get(CASH, ZERO) + _cost(bal), "no value sample; positions at cost"
    return best, "account value sample"


def _positions_at(account, end: float) -> dict:
    qty: dict = {}
    for f in account.fills:
        if f.ts < end:
            qty[f.symbol] = qty.get(f.symbol, ZERO) + (f.qty if f.side == "buy" else -f.qty)
    return {s: q for s, q in qty.items() if q != 0}


def monthly_statement(account, year: int, month: int, now: float, live_equity: Optional[Decimal] = None,
                      marks: Optional[dict] = None) -> dict:
    """Statement for one New York calendar month. marks: {symbol: price} at month end (optional)."""
    start, end = month_bounds(year, month)
    if start > now:
        raise ValueError(f"{year:04d}-{month:02d} has not started yet")
    if end <= account.created_at:
        raise ValueError(f"the account did not exist in {year:04d}-{month:02d}")
    current = end > now
    open_bal, close_bal = balances_before(account.ledger, start), balances_before(account.ledger, end)
    open_eq, open_src = equity_at(account, start, open_bal)
    if current and live_equity is not None:
        close_eq, close_src = D(live_equity), "live account value"
    else:
        close_eq, close_src = equity_at(account, end, close_bal)
    txns = [t for t in account.ledger.transactions if start <= t.ts < end]
    get = lambda t, acct: sum((v for a, v in t.postings if a == acct), ZERO)

    deposits = [{"ts": t.ts, "time": iso(t.ts), "memo": t.memo, "amount": -get(t, CAPITAL)} for t in txns if get(t, CAPITAL)]
    other = [{"ts": t.ts, "time": iso(t.ts), "kind": t.kind, "memo": t.memo, "amount": get(t, CASH)}
             for t in txns if t.kind not in TRADE_KINDS and not get(t, CAPITAL)]
    by_kind: dict = {}
    for o in other:
        by_kind[o["kind"]] = by_kind.get(o["kind"], ZERO) + o["amount"]
    cash_by_ref = {t.ref: get(t, CASH) for t in txns if t.ref}
    realized_by_ref = {t.ref: -get(t, REALIZED) for t in txns if t.ref}
    trades = [{"ts": f.ts, "time": iso(f.ts), "id": f.id, "order_id": f.order_id, "symbol": f.symbol,
               "description": describe(f.symbol), "side": f.side, "qty": f.qty, "price": f.price, "fee": f.fee,
               "kind": f.kind, "net_cash": cash_by_ref.get(f.id, ZERO), "realized": realized_by_ref.get(f.id, ZERO)}
              for f in account.fills if start <= f.ts < end]

    fees = sum((get(t, FEES) for t in txns), ZERO)
    realized = -sum((get(t, REALIZED) for t in txns), ZERO)
    open_unreal = open_eq - open_bal.get(CASH, ZERO) - _cost(open_bal)
    close_unreal = close_eq - close_bal.get(CASH, ZERO) - _cost(close_bal)
    dep_total = sum((d["amount"] for d in deposits), ZERO)
    other_total = sum((o["amount"] for o in other), ZERO)

    positions = []
    for sym, q in sorted(_positions_at(account, end).items()):
        cost = close_bal.get(f"pos:{sym}", ZERO)
        mark = (marks or {}).get(sym)
        mv = cents(q * mark * multiplier(sym)) if mark is not None else None
        positions.append({"symbol": sym, "description": describe(sym), "qty": q, "cost_basis": cost, "mark": mark,
                          "market_value": mv, "unrealized_pnl": mv - cost if mv is not None else None})
    return {
        "account_id": account.id, "account_name": account.name, "account_type": account.type,
        "month": f"{year:04d}-{month:02d}", "period_start": start, "period_end": end,
        "period_start_iso": iso(start), "period_end_iso": iso(end), "current": current,
        "generated_at": now, "generated_iso": iso(now), "lot_method": account.lot_method,
        "opening": {"cash": open_bal.get(CASH, ZERO), "cost_basis": _cost(open_bal), "unrealized_pnl": open_unreal,
                    "account_value": open_eq, "source": open_src},
        "closing": {"cash": close_bal.get(CASH, ZERO), "cost_basis": _cost(close_bal), "unrealized_pnl": close_unreal,
                    "account_value": close_eq, "source": close_src},
        "deposits": deposits, "deposits_total": dep_total,
        "trades": trades, "trade_count": len(trades),
        "bought": sum((cents(t["qty"] * t["price"] * multiplier(t["symbol"])) for t in trades if t["side"] == "buy"), ZERO),
        "sold": sum((cents(t["qty"] * t["price"] * multiplier(t["symbol"])) for t in trades if t["side"] == "sell"), ZERO),
        "fees": fees, "other": other, "other_by_kind": by_kind, "other_total": other_total,
        "realized_pnl": realized, "unrealized_change": close_unreal - open_unreal,
        "reconciliation_difference": close_eq - (open_eq + dep_total + realized - fees + other_total + close_unreal - open_unreal),
        "positions": positions,
        "notes": ["All prices are simulated. This is a paper trading statement, not a brokerage record.",
                  "Account value at a month boundary is the account value sample taken at or just before it; "
                  "month-end marks come from the simulated feed's last price before the month end.",
                  "Other income and charges lists any ledger entry that is not a trade or deposit, such as dividends or interest."],
    }


# ---- printable HTML ---------------------------------------------------------------------------------
def _m(v) -> str:
    if v is None:
        return "–"
    v = D(v)
    return ("-" if v < 0 else "") + f"{abs(cents(v)):,.2f}"


def _q(v) -> str:
    return f"{D(v).normalize():f}"


def _d(ts: float) -> str:
    return escape(ny_date(ts).isoformat())


def statement_html(st: dict) -> str:
    e = escape
    rows = lambda items: "".join(items) or '<tr><td colspan="9" class="empty">None in this period.</td></tr>'
    summary = [
        ("Opening account value", st["opening"]["account_value"]), ("Deposits and withdrawals", st["deposits_total"]),
        ("Realised profit and loss", st["realized_pnl"]), ("Fees", -st["fees"]),
        ("Other income and charges", st["other_total"]), ("Change in unrealised profit and loss", st["unrealized_change"]),
        ("Closing account value", st["closing"]["account_value"]),
    ]
    trades = [f"<tr><td>{_d(t['ts'])}</td><td>{e(t['description'])}</td><td>{e(t['side'])}</td><td>{e(t['kind'])}</td>"
              f"<td class=n>{_q(t['qty'])}</td><td class=n>{_q(t['price'])}</td><td class=n>{_m(t['fee'])}</td>"
              f"<td class=n>{_m(t['realized'])}</td><td class=n>{_m(t['net_cash'])}</td></tr>" for t in st["trades"]]
    pos = [f"<tr><td>{e(p['description'])}</td><td class=n>{_q(p['qty'])}</td><td class=n>{_m(p['cost_basis'])}</td>"
           f"<td class=n>{'–' if p['mark'] is None else _q(p['mark'])}</td><td class=n>{_m(p['market_value'])}</td>"
           f"<td class=n>{_m(p['unrealized_pnl'])}</td></tr>" for p in st["positions"]]
    flows = [f"<tr><td>{_d(d['ts'])}</td><td>{e(d['memo'])}</td><td class=n>{_m(d['amount'])}</td></tr>" for d in st["deposits"]]
    flows += [f"<tr><td>{_d(o['ts'])}</td><td>{e(o['memo'])} ({e(o['kind'])})</td><td class=n>{_m(o['amount'])}</td></tr>" for o in st["other"]]
    notes = "".join(f"<li>{e(n)}</li>" for n in st["notes"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Statement {e(st['month'])} · {e(st['account_name'])}</title>
<style>
body {{ font: 13px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: #111; background: #fff; margin: 0 auto; padding: 24px 16px; max-width: 960px; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }} h2 {{ font-size: 14px; margin: 22px 0 6px; border-bottom: 1px solid #ccc; padding-bottom: 3px; }}
.paper {{ display: inline-block; background: #fff4d6; border: 1px solid #d9b45a; border-radius: 4px; padding: 2px 8px; font-weight: 600; font-size: 12px; }}
.meta {{ color: #555; margin: 6px 0 0; }}
.wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
th, td {{ text-align: left; padding: 4px 8px; border-bottom: 1px solid #e3e3e3; white-space: nowrap; }}
th {{ font-weight: 600; color: #444; font-size: 12px; }} .n {{ text-align: right; }}
.summary td:last-child {{ text-align: right; }} .summary tr:last-child td {{ font-weight: 700; border-top: 2px solid #999; }}
.empty {{ color: #777; }} ul {{ color: #555; padding-left: 18px; }}
@media print {{ body {{ padding: 0; }} .noprint {{ display: none; }} }}
</style></head><body>
<p class="paper">Paper trading: simulated, no real money</p>
<h1>Monthly statement · {e(st['month'])}</h1>
<p class="meta">{e(st['account_name'])} ({e(st['account_id'])}, {e(st['account_type'])} account) ·
period {_d(st['period_start'])} to {_d(st['period_end'] - 1)} (New York time){' · month in progress' if st['current'] else ''} ·
tax-lot method {e(st['lot_method'].replace('_', ' '))} · generated {e(st['generated_iso'])}</p>
<p class="noprint"><button onclick="window.print()">Print</button></p>
<h2>Summary</h2>
<table class="summary"><tbody>{''.join(f'<tr><td>{e(k)}</td><td>{_m(v)}</td></tr>' for k, v in summary)}</tbody></table>
<h2>Balances</h2>
<div class="wrap"><table><thead><tr><th></th><th class=n>Cash</th><th class=n>Cost basis</th><th class=n>Unrealised</th><th class=n>Account value</th><th>Source</th></tr></thead><tbody>
<tr><td>Opening</td><td class=n>{_m(st['opening']['cash'])}</td><td class=n>{_m(st['opening']['cost_basis'])}</td><td class=n>{_m(st['opening']['unrealized_pnl'])}</td><td class=n>{_m(st['opening']['account_value'])}</td><td>{e(st['opening']['source'])}</td></tr>
<tr><td>Closing</td><td class=n>{_m(st['closing']['cash'])}</td><td class=n>{_m(st['closing']['cost_basis'])}</td><td class=n>{_m(st['closing']['unrealized_pnl'])}</td><td class=n>{_m(st['closing']['account_value'])}</td><td>{e(st['closing']['source'])}</td></tr>
</tbody></table></div>
<h2>Deposits, withdrawals and other income</h2>
<div class="wrap"><table><thead><tr><th>Date</th><th>Entry</th><th class=n>Amount</th></tr></thead><tbody>{rows(flows)}</tbody></table></div>
<h2>Trades ({st['trade_count']}) · bought {_m(st['bought'])} · sold {_m(st['sold'])} · fees {_m(st['fees'])}</h2>
<div class="wrap"><table><thead><tr><th>Date</th><th>Instrument</th><th>Side</th><th>Kind</th><th class=n>Qty</th><th class=n>Price</th><th class=n>Fee</th><th class=n>Realised</th><th class=n>Net cash</th></tr></thead>
<tbody>{rows(trades)}</tbody></table></div>
<h2>Positions at month end</h2>
<div class="wrap"><table><thead><tr><th>Instrument</th><th class=n>Qty</th><th class=n>Cost basis</th><th class=n>Mark</th><th class=n>Market value</th><th class=n>Unrealised</th></tr></thead>
<tbody>{rows(pos)}</tbody></table></div>
<h2>Notes</h2><ul>{notes}</ul>
</body></html>"""
