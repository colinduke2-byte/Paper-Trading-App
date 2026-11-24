"""CSV exports. RFC 4180 quoting, UTC ISO 8601 timestamps, Decimals written exactly as plain strings."""
import csv
import io
from decimal import Decimal

from ..options.symbology import describe
from .common import iso

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return f"{v:f}"  # never scientific notation, never a float artefact
    if isinstance(v, float):
        raise TypeError("floats are not written to CSV; convert money to Decimal and times with iso()")
    return str(v)


def text(v) -> str:
    """Free text: neutralise spreadsheet formulas by prefixing an apostrophe."""
    s = "" if v is None else str(v)
    return "'" + s if s.startswith(_FORMULA_START) else s


def to_csv(header: list, rows: list) -> str:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(header)
    for r in rows:
        w.writerow([cell(v) for v in r])
    return buf.getvalue()


def fills_csv(account) -> str:
    return to_csv(["id", "time", "order_id", "symbol", "description", "side", "qty", "price", "fee", "kind"],
                  [[f.id, iso(f.ts), f.order_id, f.symbol, describe(f.symbol), f.side, f.qty, f.price, f.fee, f.kind]
                   for f in account.fills])


def orders_csv(orders: list) -> str:
    cols = ["id", "created_at", "updated_at", "symbol", "description", "side", "qty", "type", "limit_price",
            "stop_price", "trail_amount", "trail_percent", "tif", "status", "filled_qty", "avg_fill_price", "fees",
            "parent_id", "role", "reason"]
    rows = []
    for o in sorted(orders, key=lambda o: (o.created_at, o.id)):
        rows.append([o.id, iso(o.created_at), iso(o.updated_at), o.symbol, describe(o.symbol), o.side, o.qty, o.type,
                     o.limit_price, o.stop_price, o.trail_amount, o.trail_percent, o.tif, o.status, o.filled_qty,
                     o.avg_fill_price, o.fees, o.parent_id, o.role, text(o.reason)])
    return to_csv(cols, rows)


def ledger_csv(account) -> str:
    """One row per posting, so every transaction's amounts sum to zero in the file too."""
    rows = [[t.id, iso(t.ts), t.kind, text(t.memo), t.ref, a, v]
            for t in account.ledger.transactions for a, v in t.postings]
    return to_csv(["transaction_id", "time", "kind", "memo", "ref", "account", "amount"], rows)


def positions_csv(rows: list) -> str:
    return to_csv(["symbol", "description", "qty", "avg_cost", "mark", "market_value", "cost_basis", "unrealized_pnl"],
                  [[r["symbol"], r["description"], r["qty"], r["avg_cost"], r["mark"], r["market_value"], r["cost_basis"],
                    r["unrealized_pnl"]] for r in rows])


def gains_csv(report: dict) -> str:
    cols = ["lot_id", "symbol", "description", "direction", "asset", "qty", "open_date", "close_date", "opened_at",
            "closed_at", "proceeds", "cost", "gain", "term", "wash_sale_flag", "lot_method", "close_kind", "order_id"]
    return to_csv(cols, [[r["id"], r["symbol"], r["description"], r["direction"], r["asset"], r["qty"], r["open_date"],
                          r["close_date"], r["opened_iso"], r["closed_iso"], r["proceeds"], r["cost"], r["gain"], r["term"],
                          r["wash_sale"], r["lot_method"], r["kind"], r["order_id"]] for r in report["rows"]])


def journal_csv(entries: list) -> str:
    return to_csv(["id", "target_type", "target_id", "created_at", "updated_at", "tags", "note"],
                  [[j["id"], j["target_type"], j["target_id"], iso(j["created_at"]), iso(j["updated_at"]),
                    text(";".join(j["tags"])), text(j["note"])] for j in entries])
