"""News, calendars, corporate actions and fundamentals from the configured providers.

Every method returns an envelope so the UI can always say where data came from and how
fresh it is:

    {"source": "Alpaca" | "Finnhub" | None, "as_of": unix seconds | None, "available": bool,
     "stale": bool, "reason": str | None, "items": [...]}     (fundamentals: "data": {...})

Corporate actions are exposed as a neutral list of records for the dividend/split
processing stream; see docs/DATA_PROVIDERS.md for the exact record shape.
"""
import logging
import threading
import time
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable, Optional

from ..core import calendar as cal
from .http import ProviderError, dec, parse_ts, web_url

log = logging.getLogger("paperdesk.research")

# Alpaca response key -> record type
CA_TYPES = {
    "cash_dividends": "cash_dividend", "stock_dividends": "stock_dividend", "forward_splits": "forward_split",
    "reverse_splits": "reverse_split", "unit_splits": "unit_split", "spin_offs": "spin_off",
    "cash_mergers": "cash_merger", "stock_mergers": "stock_merger", "stock_and_cash_mergers": "stock_and_cash_merger",
    "redemptions": "redemption", "name_changes": "name_change", "worthless_removals": "worthless_removal",
    "rights_distributions": "rights_distribution", "partial_calls": "partial_call",
    "reorganizations": "reorganization", "capital_gains_distributions": "capital_gains_distribution",
}
_NUMERIC = {"rate", "old_rate", "new_rate", "source_rate", "cash_rate", "acquirer_rate", "acquiree_rate",
            "alternate_rate", "price", "dividend_rate", "long_term_rate", "short_term_rate"}


def corporate_action_records(resp: dict) -> list:
    """Alpaca /v1/corporate-actions -> flat records sorted by ex/effective/process date.

    Every record has: type, id, symbol (the affected listing; for mergers the acquiree,
    for spin-offs the source), ex_date, record_date, payable_date, process_date,
    effective_date (None where not applicable), currency, source="alpaca", plus all other
    provider fields. Rate fields are Decimal.
    """
    out = []
    for key, rows in (resp or {}).items():
        kind = CA_TYPES.get(key, key.rstrip("s"))
        for r in rows or []:
            rec = {k: (dec(v) if k in _NUMERIC and v is not None else v) for k, v in r.items()}
            sym = r.get("symbol") or r.get("acquiree_symbol") or r.get("source_symbol") or r.get("old_symbol")
            rec.update(type=kind, symbol=sym, source="alpaca")
            for k in ("ex_date", "record_date", "payable_date", "process_date", "effective_date"):
                rec.setdefault(k, None)
            rec.setdefault("currency", "")
            out.append(rec)
    return sorted(out, key=lambda r: r["ex_date"] or r["effective_date"] or r["process_date"] or "")


def news_records(rows: list) -> list:
    """Alpaca /v1beta1/news articles -> plain records (newest first)."""
    out = []
    for r in rows or []:
        images = {i.get("size"): web_url(i.get("url")) for i in r.get("images") or [] if isinstance(i, dict)}
        out.append({"id": r.get("id"), "headline": r.get("headline") or "", "summary": r.get("summary") or "",
                    "source": r.get("source") or "", "url": web_url(r.get("url")), "author": r.get("author") or "",
                    "created_at": parse_ts(r.get("created_at")), "updated_at": parse_ts(r.get("updated_at")),
                    "symbols": r.get("symbols") or [], "image": images.get("thumb") or images.get("small") or None,
                    "provider": "alpaca"})
    return sorted(out, key=lambda r: r["created_at"] or 0, reverse=True)


def envelope(source, items=None, *, as_of=None, available=True, stale=False, reason=None, data=None) -> dict:
    out = {"source": source, "as_of": as_of, "available": available, "stale": stale, "reason": reason}
    if data is not None or items is None:
        out["data"] = data
    if items is not None:
        out["items"] = items
    return out


class _Cache:
    """At most MAX_ROWS entries (keys include symbols and date windows from requests); the entry fetched
    longest ago is dropped first."""
    MAX_ROWS = 512

    def __init__(self, clock):
        self.clock, self.rows, self.lock = clock, {}, threading.Lock()

    def get(self, key, ttl, fetch: Callable[[], object]):
        """(value, fetched_at, stale, error). Serves the last good value when a refresh fails."""
        now = self.clock()
        with self.lock:
            hit = self.rows.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1], hit[0], False, None
        try:
            value = fetch()
        except ProviderError as e:
            if hit:
                return hit[1], hit[0], True, e.message
            return None, None, True, e.message
        with self.lock:
            self.rows.pop(key, None)  # re-inserting moves the key to the newest end
            self.rows[key] = (now, value)
            while len(self.rows) > self.MAX_ROWS:
                del self.rows[next(iter(self.rows))]
        return value, now, False, None


class ResearchHub:
    """Alpaca for news and corporate actions, Finnhub (optional) for earnings and fundamentals."""

    def __init__(self, alpaca=None, finnhub=None, clock=time.time, simulated_reason: Optional[str] = None):
        self.alpaca, self.finnhub, self.clock = alpaca, finnhub, clock
        self.simulated_reason = simulated_reason
        self.cache = _Cache(clock)

    def sources(self) -> dict:
        return {"news": "Alpaca" if self.alpaca else "Finnhub" if self.finnhub else None,
                "corporate_actions": "Alpaca" if self.alpaca else None,
                "earnings": "Finnhub" if self.finnhub else None,
                "fundamentals": "Finnhub" if self.finnhub else None,
                "reason": self.simulated_reason}

    def _none(self, what: str, key_hint: str) -> dict:
        reason = self.simulated_reason or f"No {what} provider is configured. {key_hint}"
        return envelope(None, [], available=False, reason=reason)

    def _wrap(self, source, value, at, stale, err, key="items") -> dict:
        if value is None:
            return envelope(source, [] if key == "items" else None, available=False, stale=True,
                            reason=f"{source} data is unavailable right now: {err}")
        reason = f"Showing saved data; the latest refresh failed: {err}" if stale else None
        if key == "items":
            return envelope(source, value, as_of=at, stale=stale, reason=reason)
        return envelope(source, None, as_of=at, stale=stale, reason=reason, data=value)

    def today(self) -> date:
        return cal.ny_date(self.clock())

    def news(self, symbol: str, limit: int = 20) -> dict:
        if self.alpaca:
            v = self.cache.get(("news", symbol, limit), 300, lambda: news_records(self.alpaca.news(symbol, limit, max_wait=5)))
            return self._wrap("Alpaca", *v)
        if self.finnhub:
            t = self.today()
            v = self.cache.get(("fnews", symbol), 300, lambda: self.finnhub.news(symbol, t - timedelta(days=14), t)[:limit])
            return self._wrap("Finnhub", *v)
        return self._none("news", "Set APCA_API_KEY_ID and APCA_API_SECRET_KEY.")

    def corporate_actions(self, symbol: str, start: Optional[date] = None, end: Optional[date] = None,
                          types: Optional[str] = None) -> dict:
        if not self.alpaca:
            return self._none("corporate actions", "Set APCA_API_KEY_ID and APCA_API_SECRET_KEY.")
        t = self.today()
        start, end = start or t - timedelta(days=365), end or t + timedelta(days=90)
        v = self.cache.get(("ca", symbol, start, end, types), 3600, lambda: corporate_action_records(
            self.alpaca.corporate_actions(symbol, start.isoformat(), end.isoformat(), types, max_wait=5)))
        return self._wrap("Alpaca", *v)

    def earnings(self, symbol: str) -> dict:
        if not self.finnhub:
            return self._none("earnings calendar", "Set FINNHUB_API_KEY for earnings dates (optional).")
        t = self.today()
        # The free tier covers one month of history plus upcoming dates.
        v = self.cache.get(("earn", symbol), 3600, lambda: self.finnhub.earnings(symbol, t - timedelta(days=30),
                                                                                 t + timedelta(days=120)))
        return self._wrap("Finnhub", *v)

    def calendar(self, symbol: str) -> dict:
        ca = self.corporate_actions(symbol, types="cash_dividend,forward_split,reverse_split")
        divs = dict(ca, items=[r for r in ca.get("items", []) if r["type"] == "cash_dividend"])
        splits = dict(ca, items=[r for r in ca.get("items", []) if r["type"] in ("forward_split", "reverse_split")])
        return {"symbol": symbol, "earnings": self.earnings(symbol), "dividends": divs, "splits": splits}

    def fundamentals(self, symbol: str) -> dict:
        if not self.finnhub:
            return self._none("fundamentals", "Set FINNHUB_API_KEY for company fundamentals (optional).")
        from .finnhub import fundamentals_record
        v = self.cache.get(("fund", symbol), 6 * 3600, lambda: fundamentals_record(self.finnhub.profile(symbol),
                                                                                  self.finnhub.metrics(symbol)))
        return self._wrap("Finnhub", *v, key="data")
