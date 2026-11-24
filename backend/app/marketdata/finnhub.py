"""Finnhub free tier: earnings calendar, company profile, basic financials, company news.

Verified on finnhub.io/docs/api (2026-11-24): base https://finnhub.io/api/v1; the key goes in
the X-Finnhub-Token header (or a token= query parameter, which this module never uses so
the key stays out of URLs); 429 when over the limit plus a 30 calls/second cap on every plan.
/stock/dividend and /stock/candle are marked "Premium Access Required" and are not used.
The free plan's per-minute limit is not on any page that loads without JavaScript
(UNVERIFIED; commonly quoted as 60/min), so the default budget here is a cautious 50/min.
"""
import time
from datetime import date, timedelta
from typing import Optional

from .http import JsonClient, RateLimiter, Transport, dec, parse_ts, safe_url

BASE_URL = "https://finnhub.io/api/v1"


class FinnhubClient:
    def __init__(self, token: str, transport: Optional[Transport] = None, rpm: float = 50, sleep=time.sleep,
                 wall_clock=time.time):
        self.http = JsonClient("Finnhub", {"X-Finnhub-Token": token, "Accept": "application/json",
                                           "User-Agent": "PaperDesk"}, transport, RateLimiter(rpm),
                               wall_clock=wall_clock, sleep=sleep)

    def earnings(self, symbol: str, start: date, end: date) -> list:
        j = self.http.get(BASE_URL, "/calendar/earnings",
                          {"from": start.isoformat(), "to": end.isoformat(), "symbol": symbol}, max_wait=5) or {}
        return earnings_records(j)

    def profile(self, symbol: str) -> dict:
        return self.http.get(BASE_URL, "/stock/profile2", {"symbol": symbol}, max_wait=5) or {}

    def metrics(self, symbol: str) -> dict:
        return self.http.get(BASE_URL, "/stock/metric", {"symbol": symbol, "metric": "all"}, max_wait=5) or {}

    def news(self, symbol: str, start: date, end: date) -> list:
        j = self.http.get(BASE_URL, "/company-news",
                          {"symbol": symbol, "from": start.isoformat(), "to": end.isoformat()}, max_wait=5) or []
        return news_records(j)


def earnings_records(j: dict) -> list:
    out = []
    for r in (j or {}).get("earningsCalendar") or []:
        out.append({"type": "earnings", "symbol": r.get("symbol"), "date": r.get("date"),
                    "hour": {"bmo": "before open", "amc": "after close", "dmh": "during market hours"}.get(r.get("hour") or "", ""),
                    "quarter": r.get("quarter"), "year": r.get("year"),
                    "eps_estimate": dec(r.get("epsEstimate")), "eps_actual": dec(r.get("epsActual")),
                    "revenue_estimate": dec(r.get("revenueEstimate")), "revenue_actual": dec(r.get("revenueActual")),
                    "source": "finnhub"})
    return sorted(out, key=lambda r: r["date"] or "")


def news_records(rows: list) -> list:
    out = []
    for r in rows or []:
        out.append({"id": r.get("id"), "headline": r.get("headline") or "", "summary": r.get("summary") or "",
                    "source": r.get("source") or "", "url": safe_url(r.get("url")), "author": "",
                    "created_at": float(r["datetime"]) if r.get("datetime") is not None else None,
                    "symbols": [s for s in (r.get("related") or "").split(",") if s], "image": safe_url(r.get("image")) or None,
                    "provider": "finnhub"})
    return sorted(out, key=lambda r: r["created_at"] or 0, reverse=True)


def fundamentals_record(profile: dict, metrics: dict) -> dict:
    m = (metrics or {}).get("metric") or {}
    p = profile or {}
    mc = dec(p.get("marketCapitalization"))
    so = dec(p.get("shareOutstanding"))
    return {"name": p.get("name"), "industry": p.get("finnhubIndustry"), "exchange": p.get("exchange"),
            "country": p.get("country"), "currency": p.get("currency"), "ipo": p.get("ipo"), "website": p.get("weburl"),
            "logo": p.get("logo"),
            # Finnhub reports market cap and shares outstanding in millions.
            "market_cap": mc * 1_000_000 if mc is not None else None,
            "shares_outstanding": so * 1_000_000 if so is not None else None,
            "high_52w": dec(m.get("52WeekHigh")), "low_52w": dec(m.get("52WeekLow")), "beta": dec(m.get("beta")),
            "pe": dec(m.get("peTTM") if m.get("peTTM") is not None else m.get("peBasicExclExtraTTM")),
            "eps": dec(m.get("epsTTM")), "dividend_yield_pct": dec(m.get("dividendYieldIndicatedAnnual")),
            "avg_volume_10d_millions": dec(m.get("10DayAverageTradingVolume")), "source": "finnhub"}
