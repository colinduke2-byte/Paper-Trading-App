"""Research routes: news, earnings/dividend calendar, corporate actions, provider fundamentals.

Every response is an envelope with source, as_of, available, stale and reason so the UI
can always label where the data came from (see app/marketdata/research.py). On simulated data the
feed carries a SimResearch hub (app/marketdata/simresearch) with the same methods, plus market-wide
news, financial statements and economic and earnings calendars; a live hub without those says so.
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import Query

from ..core import calendar as cal
from ..marketdata.config import wanted_symbols
from ..marketdata.research import ResearchHub, envelope

MAX_CALENDAR_DAYS = 400


def register(app, ctx):
    def hub() -> ResearchHub:
        feed = ctx.ex.feed
        # A live feed also streams what open browser tabs are showing; install that hook lazily
        # so it follows ctx.ex if the exchange is replaced.
        if hasattr(feed, "extra_interest") and getattr(feed, "_ctx_hooked", None) is not ctx:
            feed.extra_interest = lambda: [s for sub in list(ctx.clients.values()) for s in sub.get("symbols") or []]
            # Live symbols only, also during a replay. The feed's poller thread calls this, so it goes
            # through the exchange lock (ctx.live_interest) instead of walking exchange state itself.
            feed.interest_source = getattr(ctx, "live_interest", None) or (lambda: wanted_symbols(ctx.live))
            feed._ctx_hooked = ctx
        h = getattr(feed, "research", None)
        if h is None:
            reason = ("The synthetic feed uses fictional companies, so there is no real news, calendar or "
                      "fundamental data for them." if feed.simulated else "This feed has no research data.")
            h = ResearchHub(simulated_reason=reason)
        if getattr(h, "simulated", False):
            h.clock = ctx.ex.now  # simulated research follows the exchange clock (also in a replay)
        return h

    def optional(name: str, what: str):
        """A hub method only the simulated hub has, or a function returning an unavailable envelope."""
        h = hub()
        f = getattr(h, name, None)
        if f is not None:
            return f
        reason = h.simulated_reason or f"The configured data provider does not supply {what} in this build."
        return lambda *a, **k: envelope(None, None if name == "statements" else [], available=False, reason=reason)

    def window(start: Optional[str], end: Optional[str], back: int, ahead: int):
        today = cal.ny_date(ctx.ex.now())
        s = parse_day(start, "start") or today - timedelta(days=back)
        e = parse_day(end, "end") or today + timedelta(days=ahead)
        if e < s:
            raise ValueError("end must be on or after start")
        if (e - s).days > MAX_CALENDAR_DAYS:
            raise ValueError(f"a calendar can cover at most {MAX_CALENDAR_DAYS} days")
        return s, e

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

    @app.get("/api/research/news")
    def market_news(limit: int = Query(30, ge=1, le=100), category: Optional[str] = None):
        return ctx.plain(optional("market_news", "market news")(limit, category))

    @app.get("/api/research/economic-calendar")
    def economic_calendar(start: Optional[str] = None, end: Optional[str] = None):
        s, e = window(start, end, 14, 45)
        return ctx.plain(optional("economic_calendar", "an economic calendar")(s, e))

    @app.get("/api/research/earnings-calendar")
    def earnings_calendar(start: Optional[str] = None, end: Optional[str] = None):
        s, e = window(start, end, 14, 75)
        return ctx.plain(optional("earnings_calendar", "a market-wide earnings calendar")(s, e))

    @app.get("/api/research/statements/{symbol}")
    def statements(symbol: str):
        return ctx.plain(optional("statements", "financial statements")(symbol_of(symbol)))

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
