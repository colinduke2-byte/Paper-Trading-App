"""Multi-leg option orders: preview and submit.

Request body for both routes:
    {"legs": [{"symbol": "...", "side": "buy" | "sell", "ratio": 1}, ...],   two to four legs
     "qty": 1, "type": "market" | "limit", "limit_price": "1.25", "price_effect": "debit" | "credit",
     "tif": "day" | "gtc" | "gtd" | "ioc" | "fok", "good_till_date": "2026-12-31", "client_id": "..."}

The same body is also accepted by POST /api/accounts/{id}/orders and its /preview route.
"""
from fastapi import Body

from ..options.symbology import describe


def register(app, ctx) -> None:
    def order_row(o) -> dict:
        row = ctx.plain(o)
        row["description"] = describe(o.symbol)
        row["legs"] = [dict(leg, description=describe(leg["symbol"])) for leg in o.legs or []]
        return row

    @app.post("/api/accounts/{aid}/multileg/preview")
    def preview_multileg(aid: str, body: dict = Body(...)):
        ex = ctx.ex
        ex.account(aid)
        if body.get("legs") is None:
            raise ValueError("a multi-leg order needs a list of legs")
        return ctx.plain(ex.preview_multileg(aid, body))

    @app.post("/api/accounts/{aid}/multileg")
    def submit_multileg(aid: str, body: dict = Body(...)):
        ex = ctx.ex
        ex.account(aid)
        if body.get("legs") is None:
            raise ValueError("a multi-leg order needs a list of legs")
        return order_row(ex.submit(aid, body)[0])
