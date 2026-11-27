"""Alerts (every type) and the notification centre.

The old /api/alerts routes in server.py still work: they list every alert and create or delete
global price alerts. The routes here cover every alert type, editing, pausing and notifications.
"""
from typing import Optional

from fastapi import Body, Query

from ..alerts import model
from ..core.errors import NotFound
from ..alerts.notifications import CATEGORIES, SEVERITIES
from ..marketdata.feed import TIMEFRAMES


def register(app, ctx) -> None:
    def row(al: dict) -> dict:
        out = dict(al, description=model.describe(al))
        out["params"] = {k: v for k, v in (al.get("params") or {}).items() if k != "seen"}
        return out

    @app.get("/api/alert-rules/meta")
    def alert_meta():
        ex = ctx.ex
        return {"kinds": list(model.KINDS), "timeframes": list(TIMEFRAMES), "indicators": list(model.INDICATORS),
                "account_metrics": model.ACCOUNT_METRICS, "position_metrics": model.POSITION_METRICS,
                "option_metrics": model.OPTION_METRICS, "calendar_events": list(model.CALENDAR_EVENTS),
                "default_cooldown": model.DEFAULT_COOLDOWN, "max_alerts": model.MAX_ALERTS,
                "earnings": False,
                "earnings_note": "Earnings dates are not available from the data feed, so there is no earnings alert.",
                "categories": list(CATEGORIES), "severities": list(SEVERITIES), "now": ex.now()}

    @app.get("/api/alert-rules")
    def list_alerts(account_id: Optional[str] = None):
        with ctx.lock:
            return ctx.plain([row(a) for a in ctx.ex.alerting.list(account_id)])

    @app.post("/api/alert-rules")
    def create_alert(body: dict = Body(...)):
        with ctx.lock:
            return ctx.plain(row(ctx.ex.alerting.create(body)))

    @app.patch("/api/alert-rules/{alert_id}")
    def update_alert(alert_id: str, body: dict = Body(...)):
        with ctx.lock:
            return ctx.plain(row(ctx.ex.alerting.update(alert_id, body)))

    @app.delete("/api/alert-rules/{alert_id}")
    def delete_alert(alert_id: str):
        with ctx.lock:
            ctx.ex.alerting.get(alert_id)
            ctx.ex.alerting.delete(alert_id)
            return {"ok": True}

    # ---- notifications --------------------------------------------------------------------
    def _account(aid: str) -> str:
        if aid not in ctx.ex.accounts:
            raise NotFound(f"no such account {aid}")
        return aid

    @app.get("/api/notifications")
    def notifications(account_id: str, category: str = "", severity: str = "", unread: bool = False,
                      after: str = "", limit: int = Query(100, ge=1, le=500)):
        with ctx.lock:
            n = ctx.ex.alerting.notices
            if account_id not in ctx.ex.accounts:
                # Not an error: right after a switch between live and a replay the page can still ask for an
                # account of the other exchange. It has no notifications here.
                return {"items": [], "unread": 0, "mode": ctx.mode, "account_found": False}
            return {"items": n.list(account_id, category or None, severity or None, unread, after or None, limit),
                    "unread": n.unread(account_id), "mode": ctx.mode, "account_found": True}

    @app.post("/api/notifications/read")
    def mark_read(body: dict = Body(...)):
        with ctx.lock:
            aid = _account(str(body.get("account_id", "")))
            ids = body.get("ids")
            if ids is not None and not isinstance(ids, list):
                raise ValueError("ids must be a list of notification ids")
            return {"unread": ctx.ex.alerting.notices.mark_read(aid, ids, bool(body.get("read", True)))}

    @app.delete("/api/notifications")
    def clear_notifications(account_id: str):
        with ctx.lock:
            ctx.ex.alerting.notices.clear(_account(account_id))
            return {"ok": True, "unread": 0}
