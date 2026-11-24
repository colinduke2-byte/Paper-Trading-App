"""Research routes: news, earnings/dividend calendar, corporate actions, provider fundamentals.

Every response is an envelope with source, as_of, available, stale and reason so the UI
can always label where the data came from (see app/marketdata/research.py).
"""
from datetime import date
from typing import Optional

from fastapi import HTTPException, Query, Request

from ..marketdata.config import wanted_symbols
from ..marketdata.research import ResearchHub
from .hardening import ClientRate


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

    rate = ClientRate()

    def budget(request: Request, h: ResearchHub) -> ResearchHub:
        """Provider-backed GETs are rate limited per client: each distinct request can cost provider calls,
        and a GET can be triggered by any page the browser opens while the loopback bypass is on."""
        if h.configured:
            wait = rate.allow(request.client.host if request.client else "")
            if wait:
                secs = max(1, round(wait))
                raise HTTPException(429, f"Too many research requests; try again in {secs} seconds.",
                                    headers={"Retry-After": str(secs)})
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
    def news(request: Request, symbol: str, limit: int = Query(20, ge=1, le=50)):
        sym = symbol_of(symbol)
        return ctx.plain(budget(request, hub()).news(sym, limit))

    @app.get("/api/research/calendar/{symbol}")
    def calendar(request: Request, symbol: str):
        sym = symbol_of(symbol)
        return ctx.plain(budget(request, hub()).calendar(sym))

    @app.get("/api/research/corporate-actions/{symbol}")
    def corporate_actions(request: Request, symbol: str, start: Optional[str] = Query(None, max_length=10),
                          end: Optional[str] = Query(None, max_length=10), types: Optional[str] = Query(None, max_length=400)):
        sym, first, last = symbol_of(symbol), parse_day(start, "start"), parse_day(end, "end")
        return ctx.plain(budget(request, hub()).corporate_actions(sym, first, last, types))

    @app.get("/api/research/fundamentals/{symbol}")
    def fundamentals(request: Request, symbol: str):
        sym = symbol_of(symbol)
        return ctx.plain(budget(request, hub()).fundamentals(sym))
