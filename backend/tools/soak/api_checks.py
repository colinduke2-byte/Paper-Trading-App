"""Every read route of every account, through the HTTP API, cross-checked against the exchange.

Routes must answer 200 (never 500) and agree with each other:
- the gains report total equals the ledger's realised profit (summary realized_pnl);
- the current month's statement closes on the summary's cash and equity, and every statement
  reconciles (reconciliation_difference is zero);
- positions, CSV exports and analytics answer for empty and single-day histories as well.
"""
import csv
import io
import random

from app.analytics.common import months_between
from app.analytics.trades import gains_report

from . import invariants as inv

TOL = 0.005  # JSON numbers are floats for display; money is compared to half a cent

READ = ["", "/positions", "/orders", "/orders?status=open", "/orders?status=closed", "/fills", "/ledger", "/closed",
        "/performance", "/analytics", "/analytics?benchmark=TECX&risk_free=0.04", "/gains", "/statements",
        "/lifecycle", "/settings", "/journal", "/lot-method"]
CSV = ("fills", "orders", "ledger", "positions", "gains", "journal")


def _get(client, url):
    r = client.get(url)
    if r.status_code != 200:
        inv.fail("api_status", f"GET {url} -> {r.status_code}: {r.text[:400]}")
    return r


def check_routes(sim, client) -> None:
    ex = sim.ex
    rng = random.Random(f"api-{sim.seed}-{sim.step_no}")
    for url in ("/api/accounts", "/api/status", "/api/alerts", "/api/watchlists", "/api/corporate-actions"):
        _get(client, url)
    for aid, a in ex.accounts.items():
        base = f"/api/accounts/{aid}"
        out = {}
        for path in READ:
            out[path] = _get(client, base + path).json()
        summ = ex.summary(aid)
        # gains report == ledger realised
        gains = gains_report(a)["totals"]["gain"]
        if gains != a.realized:
            inv.fail("gains_vs_ledger", f"{aid} gains report total {gains} != ledger realised {a.realized}")
        if abs(out["/gains"]["totals"]["gain"] - float(a.realized)) > TOL:
            inv.fail("gains_route_vs_ledger", f"{aid} /gains total {out['/gains']['totals']['gain']} != {a.realized}")
        if len(out["/positions"]) != len(a.positions()):
            inv.fail("positions_route", f"{aid} /positions has {len(out['/positions'])} rows for {len(a.positions())} positions")
        # statements: the current month closes on the live summary; every month reconciles
        months = months_between(a.created_at, ex.now())
        if out["/statements"]["months"] != months:
            inv.fail("statement_months", f"{aid} months {out['/statements']['months'][:3]} != {months[:3]}")
        pick = {months[0], months[-1]} | ({months[1]} if len(months) > 1 else set()) | {rng.choice(months)}
        for month in sorted(pick):
            st = _get(client, f"{base}/statements/{month}").json()
            _get(client, f"{base}/statements/{month}/html")
            _get(client, f"{base}/gains?month={month}")
            if abs(st["reconciliation_difference"]) > TOL:
                inv.fail("statement_reconciliation", f"{aid} {month} reconciliation difference {st['reconciliation_difference']}"
                                                     f" (other: {st['other_by_kind']})")
            if month == months[0]:
                if abs(st["closing"]["cash"] - float(a.cash)) > TOL:
                    inv.fail("statement_cash", f"{aid} {month} closing cash {st['closing']['cash']} != {a.cash}")
                if abs(st["closing"]["account_value"] - float(summ["equity"])) > TOL:
                    inv.fail("statement_equity", f"{aid} {month} closing value {st['closing']['account_value']} != "
                                                 f"summary equity {summ['equity']}")
                held = {p["symbol"]: p["qty"] for p in st["positions"]}
                real = {s: float(q) for s, q in a.positions().items()}
                if held != real:
                    inv.fail("statement_positions", f"{aid} {month} statement positions {held} != positions {real}")
        for kind in CSV:
            r = _get(client, f"{base}/export/{kind}.csv")
            rows = list(csv.reader(io.StringIO(r.text)))
            if kind == "positions" and len(rows) - 1 != len(a.positions()):
                inv.fail("csv_positions", f"{aid} positions CSV has {len(rows) - 1} rows for {len(a.positions())}")
        an = out["/analytics"]
        if abs(an["summary"]["equity"] - float(summ["equity"])) > TOL:
            inv.fail("analytics_equity", f"{aid} analytics equity {an['summary']['equity']} != {summ['equity']}")
