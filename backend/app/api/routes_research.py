"""Research routes: news, earnings/dividend calendar, corporate actions, provider fundamentals.

Every response is an envelope with source, as_of, available, stale and reason so the UI
can always label where the data came from (see app/marketdata/research.py).
"""
from datetime import date
from typing import Optional

from fastapi import Query

from ..marketdata.config import wanted_symbols
from ..marketdata.research import ResearchHub


def register(app, ctx):
    def hub() -> ResearchHub:
        feed = ctx.ex.feed
        # A live feed also streams what open browser tabs are showing; install that hook lazily
        # so it follows ctx.ex if the exchange is replaced.
        if hasattr(feed, "extra_interest") and getattr(feed, "_ctx_hooked", None) is not ctx:
            feed.extra_interest = lambda: [s for sub in list(ctx.clients.values()) for s in sub.get("symbols") or []]
            feed.interest_source = lambda: wanted_symbols(ctx.live)  # live symbols only, also during a replay
            feed._ctx_hooked = ctx
        h = getattr(feed, "research", None)
        if h is None:
            reason = ("The synthetic feed uses fictional companies, so there is no real news, calendar or "
                      "fundamental data for them." if feed.simulated else "This feed has no research data.")
            h = ResearchHub(simulated_reason=reason)
        return h

    def symbol_of(symbol: str) -> str:
        symbol = symbol.upper().strip()
        if not ctx.ex.is_stock(symbol):
            raise KeyError(f"unknown symbol {symbol}")
        return symbol

    def parse_day(v: Optional[str], name: str) -> Optional[date]:
        if not v:
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"{name} must be YYYY-MM-DD")

    hub()  # make a live stream follow open browser tabs from the start

    @app.get("/api/research/sources")
    def sources():
        return hub().sources()

    @app.get("/api/research/news/{symbol}")
    def news(symbol: str, limit: int = Query(20, ge=1, le=50)):
        return ctx.plain(hub().news(symbol_of(symbol), limit))

    @app.get("/api/research/calendar/{symbol}")
    def calendar(symbol: str):
        return ctx.plain(hub().calendar(symbol_of(symbol)))

    @app.get("/api/research/corporate-actions/{symbol}")
    def corporate_actions(symbol: str, start: Optional[str] = None, end: Optional[str] = None,
                          types: Optional[str] = None):
        return ctx.plain(hub().corporate_actions(symbol_of(symbol), parse_day(start, "start"), parse_day(end, "end"), types))

    @app.get("/api/research/fundamentals/{symbol}")
    def fundamentals(symbol: str):
        return ctx.plain(hub().fundamentals(symbol_of(symbol)))
