"""Notification centre: a persisted, capped, per-account log of what happened.

Sources:
- alerts (the engine calls `add` directly), and
- system events: everything the exchange and the lifecycle already put on `Exchange.events`
  (fills, order status changes, margin calls, liquidation steps, dividends, splits, option
  exercise / assignment / expiry). `EventStream` hands each event to `from_event` as it is
  appended, so no business code calls the notification centre itself.

Each new notification is also appended to the events stream as {"type": "notification"}, so the
WebSocket broadcast delivers it live; `/api/notifications` serves the log for catch-up.
Notifications never touch the ledger: the ledger is for money.
"""
from decimal import Decimal
from typing import Callable, Optional

from ..core import models as m
from ..core.money import D
from ..options.symbology import describe, parse as parse_option

CAP = 500  # notifications kept per account; the oldest are dropped first
SEVERITIES = ("info", "success", "warning", "critical")
CATEGORIES = ("alert", "reminder", "order", "fill", "option", "margin", "liquidation", "dividend", "split",
              "account", "data")
OPTION_KINDS = {"exercise": "exercised", "assignment": "assigned", "expiration": "expired worthless",
                "closed at expiry": "closed at expiry for its intrinsic value"}


class EventStream(list):
    """The exchange's event list. Every append is also offered to a listener (the notifier)."""

    def __init__(self, listener: Callable[[dict], None]):
        super().__init__()
        self._listener = listener

    def append(self, event) -> None:
        super().append(event)
        if isinstance(event, dict) and event.get("type") not in ("notification", "alert"):
            try:
                self._listener(event)
            except Exception as e:  # a notification must never break trading
                print(f"notification error: {e!r}")


def _num(v) -> str:
    v = D(v)
    return f"{v.normalize():f}" if v == v.to_integral_value() else f"{v:f}"


def _px(v) -> str:
    v = D(v)
    return f"{v:,.2f}" if abs(v) >= 1 or v == 0 else f"{v:f}"


class NotificationCentre:
    def __init__(self, ex):
        self.ex = ex
        self.logs: dict[str, list] = {}  # account id -> notifications, oldest first
        self.seq = 1

    # ---- writing -------------------------------------------------------------------------
    def add(self, account_id: Optional[str], category: str, severity: str, title: str, text: str,
            symbol: Optional[str] = None, order_id: Optional[str] = None, alert_id: Optional[str] = None) -> list:
        """Add a notification for one account, or for every account when account_id is None."""
        targets = [account_id] if account_id is not None else list(self.ex.accounts)
        out = []
        for aid in targets:
            if aid not in self.ex.accounts:
                continue
            n = {"id": f"N{self.seq:08d}", "ts": self.ex.now(), "account_id": aid, "category": category,
                 "severity": severity, "title": title, "text": text, "symbol": symbol, "order_id": order_id,
                 "alert_id": alert_id, "read": False}
            self.seq += 1
            log = self.logs.setdefault(aid, [])
            log.append(n)
            if len(log) > CAP:
                del log[:len(log) - CAP]
            self.ex.events.append({"type": "notification", "account_id": aid, "notification": n})
            out.append(n)
        if out:
            self.ex.dirty = True
        return out

    # ---- reading ---------------------------------------------------------------------------
    def list(self, account_id: str, category: Optional[str] = None, severity: Optional[str] = None,
             unread: bool = False, after: Optional[str] = None, limit: int = 100) -> list:
        rows = self.logs.get(account_id, [])
        if after:
            rows = [n for n in rows if n["id"] > after]
        if category:
            cats = set(category.split(","))
            rows = [n for n in rows if n["category"] in cats]
        if severity:
            sev = set(severity.split(","))
            rows = [n for n in rows if n["severity"] in sev]
        if unread:
            rows = [n for n in rows if not n["read"]]
        return rows[::-1][:limit]

    def unread(self, account_id: str) -> int:
        return sum(1 for n in self.logs.get(account_id, ()) if not n["read"])

    def mark_read(self, account_id: str, ids: Optional[list] = None, read: bool = True) -> int:
        want = None if ids is None else set(ids)
        for n in self.logs.get(account_id, ()):
            if want is None or n["id"] in want:
                n["read"] = read
        self.ex.dirty = True
        return self.unread(account_id)

    def clear(self, account_id: str) -> None:
        self.logs.pop(account_id, None)
        self.ex.dirty = True

    # ---- persistence ---------------------------------------------------------------------------
    def to_plain(self) -> dict:
        logs = {aid: rows for aid, rows in self.logs.items() if aid in self.ex.accounts}
        return {"seq": self.seq, "logs": logs}

    def restore(self, d) -> None:
        self.logs, self.seq = {}, 1
        if not isinstance(d, dict):
            return
        self.seq = int(d.get("seq", 1))
        for aid, rows in (d.get("logs") or {}).items():
            if isinstance(rows, list):
                self.logs[aid] = [r for r in rows if isinstance(r, dict) and "id" in r][-CAP:]

    # ---- system events -> notifications ------------------------------------------------------
    def from_event(self, e: dict) -> None:
        kind = e.get("type")
        if kind == "fill":
            self._fill(e["fill"])
        elif kind == "order":
            self._order(e["order"])
        elif kind == "lifecycle":
            self._lifecycle(e)

    def _fill(self, f) -> None:
        if f.kind == "trade":
            o = self.ex.engine.orders.get(f.order_id) if f.order_id else None
            verb = "Bought" if f.side == m.BUY else "Sold"
            what = describe(f.symbol)
            unit = " contracts" if parse_option(f.symbol) else ""
            if o is not None and o.legs:
                text = f"{verb} {_num(f.qty)}{unit} {what} at {_px(f.price)} (leg of order {o.id})."
                title, partial = "Order leg filled", False
            elif o is not None and o.filled_qty < o.qty:
                text = (f"{verb} {_num(f.qty)}{unit} {what} at {_px(f.price)}; {_num(o.filled_qty)} of {_num(o.qty)} "
                        f"filled so far (order {o.id}).")
                title, partial = "Order partly filled", True
            else:
                text = f"{verb} {_num(f.qty)}{unit} {what} at {_px(f.price)}" + (f" (order {o.id})." if o else ".")
                title, partial = "Order filled", False
            sev = "info" if partial else "success"
            if o is not None and o.role == "liquidation":
                title, sev = "Liquidation fill", "warning"
            self.add(f.account_id, "fill", sev, title, text,
                     symbol=f.symbol, order_id=f.order_id)
        elif f.kind in OPTION_KINDS and parse_option(f.symbol):
            # Exercise and assignment also book the stock leg; one notification per contract event is enough.
            n = _num(f.qty)
            text = f"{describe(f.symbol)}: {n} contract{'s' if f.qty != 1 else ''} {OPTION_KINDS[f.kind]}."
            if f.kind in ("exercise", "assignment"):
                c = parse_option(f.symbol)
                long_ = f.side == m.SELL  # the contract fill closes the position: a sell closes a long one
                stock_buy = (c.kind == "C") == long_  # call holders and put writers take the shares
                text += f" You {'bought' if stock_buy else 'sold'} {_num(f.qty * 100)} {c.underlying} at {_px(c.strike)}."
            title = {"exercise": "Option exercised", "assignment": "Option assigned",
                     "expiration": "Option expired", "closed at expiry": "Option closed at expiry"}[f.kind]
            self.add(f.account_id, "option", "warning" if f.kind == "assignment" else "info", title, text,
                     symbol=f.symbol)

    def _order(self, o) -> None:
        if o.status not in (m.CANCELED, m.EXPIRED, m.REJECTED):
            return
        reason = (o.reason or "").strip()
        if o.status == m.CANCELED and reason.lower() == "canceled by user":
            return  # the user did it; no need to tell them
        verb = "buy" if o.side == m.BUY else "sell"
        what = f"{o.symbol} multi-leg order" if o.legs else f"{_num(o.qty)} {describe(o.symbol)}"
        status = {m.CANCELED: "canceled", m.EXPIRED: "expired", m.REJECTED: "rejected"}[o.status]
        verb_done = "expired" if o.status == m.EXPIRED else f"was {status}"
        if reason.lower().rstrip(".") in (status, ""):
            reason = ""
        why = f": {reason[0].lower() + reason[1:].rstrip('.')}." if reason else "."
        subject = f"Order {o.id} ({what})" if o.legs else f"Order {o.id} to {verb} {what}"
        text = f"{subject} {verb_done}{why}"
        sev = "warning" if o.status == m.REJECTED or "margin call" in reason.lower() else "info"
        self.add(o.account_id, "order", sev, f"Order {status}", text, symbol=o.symbol, order_id=o.id)

    LIFECYCLE = {
        # kind: (category, severity, title)
        "margin_call": ("margin", "critical", "Margin call"),
        "liquidation": ("liquidation", "critical", "Liquidation"),
        "dividend": ("dividend", "success", "Dividend"),
        "dividend_record": ("dividend", "info", "Ex-dividend"),
        "dividend_adjustment": ("order", "info", "Order adjusted for a dividend"),
        "reinvestment": ("dividend", "info", "Dividend reinvestment"),
        "split": ("split", "info", "Stock split applied"),
        "margin interest": ("account", "info", "Margin interest"),
        "short borrow fee": ("account", "info", "Short borrow fee"),
    }

    def _lifecycle(self, e: dict) -> None:
        kind, text = e.get("kind", ""), e.get("message", "")
        cat, sev, title = self.LIFECYCLE.get(kind, ("account", "info", "Account notice"))
        if kind == "margin_call" and text.startswith("Margin call met"):
            sev, title = "success", "Margin call cleared"
        if kind == "dividend" and "charged" in text:
            sev, title = "warning", "Dividend charged"
        symbol = e.get("symbol")
        self.add(e.get("account_id"), cat, sev, title, text, symbol=symbol)
