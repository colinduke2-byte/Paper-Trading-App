"""Per-account rule settings, lifecycle state and scheduled corporate actions."""
from fastapi import Body, Query

from ..core.money import D
from ..lifecycle import settings as cfg


def register(app, ctx):
    def _settings_view(a) -> dict:
        return {"account_id": a.id, "account_type": a.type, "settings": a.settings, "defaults": cfg.defaults(),
                "fields": cfg.describe()}

    @app.get("/api/accounts/{aid}/settings")
    def get_settings(aid: str):
        return _settings_view(ctx.ex.account(aid))

    @app.put("/api/accounts/{aid}/settings")
    def put_settings(aid: str, body: dict = Body(...)):
        ex = ctx.ex
        a = ex.account(aid)
        changes = body.get("settings", body) if isinstance(body, dict) else body
        a.settings = cfg.update(a.settings, changes)
        ex.dirty = True
        return _settings_view(a)

    @app.post("/api/accounts/{aid}/settings/reset")
    def reset_settings(aid: str):
        ex = ctx.ex
        a = ex.account(aid)
        a.settings = cfg.defaults()
        ex.dirty = True
        return _settings_view(a)

    @app.get("/api/accounts/{aid}/lifecycle")
    def lifecycle_state(aid: str):
        ex = ctx.ex
        a = ex.account(aid)
        life = a.life
        return ctx.plain({
            "settled_cash": a.cash - life.unsettled_total(), "unsettled": [
                {"settle_date": d, "amount": amt, "fill_id": ref} for d, amt, ref in life.unsettled],
            "day_trades": [{"date": d, "symbol": s, "order_id": o} for d, s, o in life.day_trades],
            "day_trade_count": ex.lifecycle.day_trade_count(a),
            "pending_dividends": [dict(p, qty=D(p["qty"]), rate=D(p["rate"]), amount=D(p["amount"]))
                                  for p in life.pending_dividends],
            "accrued_interest": life.accrued_interest, "accrued_borrow_fee": life.accrued_borrow,
            "margin_call_since": life.margin_call_since, "prices_unavailable": life.prices_missing,
            "log": [{"ts": t, "kind": k, "message": msg} for t, k, msg in life.log[-100:]][::-1]})

    @app.get("/api/corporate-actions")
    def corporate_actions(symbol: str = "", days: int = Query(120, ge=1, le=730)):
        ex = ctx.ex
        acts = ex.lifecycle.upcoming(days, symbol.upper().strip() or None)
        return [dict(x.to_plain(), applied=x.id in ex.lifecycle.applied) for x in acts]
