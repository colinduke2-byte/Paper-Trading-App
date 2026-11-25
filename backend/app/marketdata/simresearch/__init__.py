"""Simulated research for the fictional companies: fundamentals, statements, earnings, news and an
economic calendar.

`SimResearch` has the same methods (and envelope shapes) as `research.ResearchHub`, so the API routes
and the UI work the same way in live and simulated mode. Every item is labelled "Simulated".

Determinism: everything scheduled (statements, estimates, results, ratings, economic releases, the
price gap each one causes) is a pure function of the feed seed and the time asked about. Items derived
from prices (large moves, 52-week highs and lows, sector moves) come from the feed's stored daily bars,
which are saved with the market state, so they survive a restart too.

Price effects (synthetic feed only): `due_moves(start, end)` gives the log return each symbol takes for
the earnings and economic releases with a time in (start, end]. The feed applies it as a gap and saves
the time it has processed up to, so each release moves the price once, including releases that fell
while the program was stopped. Releases from before the feed first started are history and move nothing.
`event_variance(symbol, expiry_ts, now)` adds the earnings gap's variance to option volatility for
expiries after an upcoming report, so implied volatility rises into the report and drops after it.
"""
import math
import re
from datetime import date, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from typing import Optional

from ...core import calendar as cal
from ...core.money import CENT, ZERO
from . import macro
from .profiles import SOURCE, Profile, ny_ts, profile, quarter_label, quarter_of, release_time, rng
from .statements import CompanyModel, per_share

NOTE = ("Simulated data. Every company, figure, headline and economic release here is generated for "
        "practice; none of it describes a real company, person or economy.")
GAP_CLIP = 3.5


def _r(x: float, places: str = "0.01") -> Optional[Decimal]:
    if x is None or not math.isfinite(x):
        return None
    return Decimal(repr(x)).quantize(Decimal(places), ROUND_HALF_UP)


def _pct(a: Decimal, b: Decimal) -> Optional[Decimal]:
    if not b or b <= 0 or a is None:
        return None
    return _r(float(a / b - 1) * 100)


def envelope(items=None, *, as_of=None, data=None, reason=None, available=True) -> dict:
    out = {"source": SOURCE, "as_of": as_of, "available": available, "stale": False, "reason": reason,
           "simulated": True}
    if items is not None:
        out["items"] = items
    else:
        out["data"] = data
    return out


class SimResearch:
    simulated = True
    simulated_reason = None

    def __init__(self, feed, seed: int, applies_moves: bool = False):
        self.feed, self.seed = feed, int(seed)
        self.applies_moves = applies_moves
        self.clock = lambda: getattr(feed, "sim_last_step", 0.0) or 0.0  # routes replace this with ex.now
        self._models: dict = {}
        self._ratings: dict = {}

    # ---- basics ------------------------------------------------------------
    def _now(self, now: Optional[float]) -> float:
        return float(self.clock() if now is None else now)

    def stocks(self) -> list:
        return [s for s, i in self.feed.info.items() if i.get("type", "stock") != "etf"]

    def profile(self, symbol: str) -> Profile:
        i = self.feed.info[symbol]
        return profile(self.seed, symbol, i.get("name", symbol), i.get("sector", ""), float(i.get("vol", 0.3)),
                       float(i.get("eps") or 0), float(self._listed_price(symbol)), float(i.get("shares_outstanding") or 0),
                       i.get("type") == "etf")

    def _listed_price(self, symbol: str) -> float:
        from ..synthetic import UNIVERSE
        for u in UNIVERSE:
            if u[0] == symbol:
                return u[3]
        q = self.feed.quote(symbol)
        return float(q.last) if q else 50.0

    def model(self, symbol: str) -> CompanyModel:
        m = self._models.get(symbol)
        if m is None:
            i = self.feed.info[symbol]
            div_total = float(i.get("dividend") or 0) * float(self.shares(symbol, None))
            m = CompanyModel(self.seed, self.profile(symbol), float(i.get("eps") or 0),
                             float(i.get("shares_outstanding") or 0), div_total)
            self._models[symbol] = m
        return m

    def moves_applied(self, ts: float) -> bool:
        return self.applies_moves and ts > float(getattr(self.feed, "sim_born", float("inf")))

    def split_factor(self, symbol: str, day: Optional[date]) -> Fraction:
        """Product of the splits of `symbol` the feed has applied with an ex-date on or before `day`."""
        f = Fraction(1)
        prefix = symbol + "-split-"
        for aid in list(self.feed.applied_actions):
            if not aid.startswith(prefix):
                continue
            try:
                ex = date.fromisoformat(aid[len(prefix):])
            except ValueError:
                continue
            if day is None or ex <= day:
                for a in self.feed.corporate_actions(ex, ex):
                    if a.id == aid:
                        f *= Fraction(a.new, a.old)
        return f

    def shares(self, symbol: str, now: Optional[float]) -> Decimal:
        base = Fraction(int(self.feed.info[symbol].get("shares_outstanding") or 0))
        day = cal.ny_date(now) if now is not None else None
        return Decimal(int(base * self.split_factor(symbol, day)))

    def price(self, symbol: str) -> Optional[Decimal]:
        q = self.feed.quote(symbol)
        return q.last if q is not None else None

    # ---- earnings -------------------------------------------------------------
    def last_reported(self, symbol: str, now: float) -> int:
        p = self.profile(symbol)
        k = quarter_of(cal.ny_date(now))
        while release_time(self.seed, p, k)[1] > now:
            k -= 1
        return k

    def gap(self, symbol: str, k: int) -> float:
        """Log return of the earnings-day gap for quarter k's report."""
        p = self.profile(symbol)
        z = self.model(symbol).surprise_z(k)
        g = rng(self.seed, symbol, "guidance", k).gauss(0, 1)
        return p.expected_move * max(min(0.75 * z + 0.66 * g, GAP_CLIP), -GAP_CLIP)

    def guidance(self, symbol: str, k: int) -> str:
        g = rng(self.seed, symbol, "guidance", k).gauss(0, 1)
        return "raised" if g > 0.8 else "cut" if g < -0.8 else "reaffirmed"

    def earnings_record(self, symbol: str, k: int, now: float) -> dict:
        p, m = self.profile(symbol), self.model(symbol)
        d, ts = release_time(self.seed, p, k)
        sh = self.shares(symbol, now)
        est = m.estimate(k, sh)
        released = ts <= now
        rec = {"type": "earnings", "symbol": symbol, "date": d.isoformat(), "hour": p.hour, "ts": ts,
               "quarter": k % 4 + 1, "year": k // 4, "period": quarter_label(k), "quarter_index": k,
               "eps_estimate": est["eps_estimate"], "revenue_estimate": est["revenue_estimate"],
               "eps_actual": None, "revenue_actual": None, "status": "reported" if released else "upcoming",
               "source": "simulated", "simulated": True,
               "expected_move_pct": _r((math.exp(p.expected_move) - 1) * 100)}
        if released:
            inc = m.quarter(k, sh)["income"]
            a, e = inc["eps"], est["eps_estimate"]
            rec.update(eps_actual=a, revenue_actual=inc["revenue"],
                       result="beat" if a > e else "miss" if a < e else "in line",
                       surprise_pct=_r(float((a - e) / abs(e)) * 100) if e else None,
                       guidance=self.guidance(symbol, k),
                       price_reaction_pct=_r((math.exp(self.gap(symbol, k)) - 1) * 100)
                       if self.moves_applied(ts) else None)
        return rec

    def earnings(self, symbol: str, now: Optional[float] = None) -> dict:
        now = self._now(now)
        if symbol not in self.stocks():
            return envelope([], as_of=now, reason="Funds do not report earnings.")
        last = self.last_reported(symbol, now)
        return envelope([self.earnings_record(symbol, k, now) for k in range(last - 3, last + 3)], as_of=now)

    def earnings_calendar(self, start: date, end: date, now: Optional[float] = None) -> dict:
        now = self._now(now)
        out = []
        for s in self.stocks():
            p = self.profile(s)
            for k in range(quarter_of(start) - 2, quarter_of(end) + 1):
                d, _ = release_time(self.seed, p, k)
                if start <= d <= end:
                    out.append(self.earnings_record(s, k, now))
        out.sort(key=lambda r: (r["ts"], r["symbol"]))
        return envelope(out, as_of=now)

    def upcoming_releases(self, symbol: str, after: float, until: float) -> list:
        """(ts, k) of earnings reports for `symbol` with after < ts <= until."""
        if symbol not in self.feed.info or self.feed.info[symbol].get("type") == "etf":
            return []
        p = self.profile(symbol)
        out = []
        k = quarter_of(cal.ny_date(after)) - 2
        while True:
            ts = release_time(self.seed, p, k)[1]
            if ts > until:
                return out
            if ts > after:
                out.append((ts, k))
            k += 1

    def event_variance(self, symbol: str, expiry_ts: float, now: float) -> float:
        """Extra total variance (log-return squared) from earnings reports between now and expiry."""
        try:
            rel = self.upcoming_releases(symbol, now, expiry_ts)
        except KeyError:
            return 0.0
        return sum(self.profile(symbol).expected_move ** 2 for _ in rel)

    # ---- price effects for the synthetic feed --------------------------------------
    def beta(self, symbol: str) -> float:
        i = self.feed.info[symbol]
        if i.get("type") != "etf":
            return self.profile(symbol).beta
        hold = self.holdings(symbol)
        tot = sum(w for _, w in hold) or 1.0
        return sum(self.profile(s).beta * w for s, w in hold) / tot

    def due_moves(self, start: float, end: float) -> dict:
        """symbol -> total log return for the releases with start < ts <= end (both kinds)."""
        out: dict = {}
        if end <= start:
            return out
        for e in macro.releases(self.seed, start, end):
            for s in self.feed.info:
                out[s] = out.get(s, 0.0) + self.beta(s) * e["market_move"]
        for s in self.stocks():
            for _, k in self.upcoming_releases(s, start, end):
                out[s] = out.get(s, 0.0) + self.gap(s, k)
        return out

    def next_release_after(self, ts: float) -> float:
        """Time of the next release of any kind after ts (for a cheap check in the tick loop)."""
        horizon = ts + 40 * 86400
        times = [e["ts"] for e in macro.releases(self.seed, ts, horizon)[:1]]
        for s in self.stocks():
            rel = self.upcoming_releases(s, ts, horizon)
            if rel:
                times.append(rel[0][0])
        return min(times) if times else horizon

    # ---- holdings (funds) -----------------------------------------------------------
    def holdings(self, symbol: str) -> list:
        """(symbol, weight) by market value for a fund: its named sector (plus Communication for
        Technology), or every stock for a broad-market fund."""
        name = self.feed.info[symbol].get("name", "")
        stocks = self.stocks()
        sectors = {self.feed.info[s].get("sector") for s in stocks}
        named = [sec for sec in sectors if sec and sec.split()[0] in name]
        if named:
            want = set(named) | ({"Communication"} if "Technology" in named else set())
            stocks = [s for s in stocks if self.feed.info[s].get("sector") in want]
        out = []
        for s in stocks:
            px = self.price(s)
            if px is not None:
                out.append((s, float(px) * float(self.shares(s, None))))
        tot = sum(w for _, w in out) or 1.0
        return sorted(((s, w / tot) for s, w in out), key=lambda r: -r[1])

    # ---- fundamentals ------------------------------------------------------------------
    def _range_52w(self, symbol: str):
        rows = self.feed.store.daily.get(symbol, [])[-252:]
        q = self.feed.quote(symbol)
        highs = [r[2] for r in rows] + ([float(q.high)] if q else [])
        lows = [r[3] for r in rows] + ([float(q.low)] if q else [])
        return (_r(max(highs)) if highs else None), (_r(min(lows)) if lows else None)

    def fundamentals(self, symbol: str, now: Optional[float] = None) -> dict:
        now = self._now(now)
        i = self.feed.info[symbol]
        px = self.price(symbol)
        hi, lo = self._range_52w(symbol)
        sh = self.shares(symbol, now)
        dps = Decimal(repr(float(i.get("dividend") or 0))).quantize(CENT, ROUND_HALF_UP)
        base = {"name": i.get("name"), "industry": i.get("sector"), "exchange": "Simulated exchange", "country": None,
                "currency": "USD", "ipo": None, "website": None, "logo": None, "high_52w": hi, "low_52w": lo,
                "dividend_per_share": dps, "dividend_yield_pct": _r(float(dps / px) * 100) if px else None,
                "beta": _r(self.beta(symbol)), "source": "simulated", "simulated": True, "price": px,
                "note": NOTE}
        if i.get("type") == "etf":
            hold = self.holdings(symbol)
            sectors: dict = {}
            for s, w in hold:
                sec = self.feed.info[s].get("sector")
                sectors[sec] = sectors.get(sec, 0.0) + w
            mcaps = [(s, float(self.price(s) or 0) * float(self.shares(s, now))) for s, _ in hold]
            earn = sum(float(self.model(s).ttm(self.last_reported(s, now), self.shares(s, now))["net_income"])
                       for s, _ in hold)
            tot_mcap = sum(v for _, v in mcaps)
            base.update(kind="fund", market_cap=(px * sh) if px else None, shares_outstanding=sh,
                        aum=(px * sh) if px else None, pe=_r(tot_mcap / earn) if earn > 0 else None, eps=None,
                        expense_ratio_pct=Decimal("0.03") if len(hold) > 12 else Decimal("0.20"),
                        holdings_count=len(hold),
                        holdings=[{"symbol": s, "name": self.feed.info[s].get("name"),
                                   "sector": self.feed.info[s].get("sector"), "weight_pct": _r(w * 100)}
                                  for s, w in hold],
                        sector_weights=[{"sector": k, "weight_pct": _r(v * 100)}
                                        for k, v in sorted(sectors.items(), key=lambda r: -r[1])])
            return envelope(data=base, as_of=now)
        m = self.model(symbol)
        k = self.last_reported(symbol, now)
        t, t_prev = m.ttm(k, sh), m.ttm(k - 4, sh)
        bs = t["balance"]
        eps = per_share(t["net_income"], sh)
        mcap = px * sh if px is not None else None
        nxt = self.earnings_record(symbol, k + 1, now)
        rating = self.current_rating(symbol, now)
        ratio = lambda a, b: _r(float(a / b)) if b else None
        pct = lambda a, b: _r(float(a / b) * 100) if b else None
        base.update(
            kind="stock", market_cap=mcap, shares_outstanding=sh, eps=eps,
            pe=_r(float(px / eps)) if px is not None and eps > 0 else None,
            ps=ratio(mcap, t["revenue"]) if mcap is not None else None,
            pb=ratio(mcap, bs["equity"]) if mcap is not None and bs["equity"] > 0 else None,
            revenue_ttm=t["revenue"], net_income_ttm=t["net_income"], free_cash_flow_ttm=t["free_cash_flow"],
            gross_margin_pct=pct(t["gross_profit"], t["revenue"]),
            operating_margin_pct=pct(t["operating_income"], t["revenue"]),
            net_margin_pct=pct(t["net_income"], t["revenue"]),
            roe_pct=pct(t["net_income"], bs["equity"]) if bs["equity"] > 0 else None,
            debt_to_equity=ratio(bs["debt"], bs["equity"]) if bs["equity"] > 0 else None,
            payout_ratio_pct=pct(t["dividends"], t["net_income"]) if t["net_income"] > 0 else None,
            revenue_growth_pct=_pct(t["revenue"], t_prev["revenue"]),
            eps_growth_pct=_pct(t["net_income"], t_prev["net_income"]) if t_prev["net_income"] > 0 else None,
            book_value_per_share=per_share(bs["equity"], sh), last_period=quarter_label(k),
            next_earnings={"date": nxt["date"], "hour": nxt["hour"], "period": nxt["period"],
                           "eps_estimate": nxt["eps_estimate"], "expected_move_pct": nxt["expected_move_pct"]},
            analyst_rating=rating)
        return envelope(data=base, as_of=now)

    def statements(self, symbol: str, now: Optional[float] = None, quarters: int = 8, years: int = 3) -> dict:
        now = self._now(now)
        if self.feed.info[symbol].get("type") == "etf":
            return envelope(data={"kind": "fund", "quarterly": [], "annual": []}, as_of=now,
                            reason="Funds do not publish company financial statements; see the holdings instead.")
        m, sh = self.model(symbol), self.shares(symbol, now)
        k = self.last_reported(symbol, now)
        last_year = (k - 3) // 4  # the latest fiscal year whose fourth quarter is reported
        q = [m.quarter(j, sh) for j in range(k, k - quarters, -1)]
        a = [m.year(y, sh) for y in range(last_year, last_year - years, -1)]
        return envelope(data={"kind": "stock", "currency": "USD", "units": "dollars",
                              "per_share_note": "Per-share figures use the current share count (adjusted for splits).",
                              "quarterly": q, "annual": a}, as_of=now)

    # ---- dividends and splits ------------------------------------------------------------
    def corporate_actions(self, symbol: str, start: Optional[date] = None, end: Optional[date] = None,
                          types: Optional[str] = None, now: Optional[float] = None) -> dict:
        now = self._now(now)
        t = cal.ny_date(now)
        start, end = start or t - timedelta(days=365), end or t + timedelta(days=90)
        want = set(types.split(",")) if types else None
        out = []
        for a in self.feed.corporate_actions(start, end):
            if a.symbol != symbol:
                continue
            if a.kind == "dividend":
                rec = {"type": "cash_dividend", "id": a.id, "symbol": a.symbol, "ex_date": a.ex_date.isoformat(),
                       "record_date": a.ex_date.isoformat(), "payable_date": a.pay_date.isoformat() if a.pay_date else None,
                       "process_date": None, "effective_date": None, "rate": a.amount, "special": False, "foreign": False}
            else:
                rec = {"type": "forward_split" if a.new > a.old else "reverse_split", "id": a.id, "symbol": a.symbol,
                       "ex_date": a.ex_date.isoformat(), "record_date": None, "payable_date": None, "process_date": None,
                       "effective_date": a.ex_date.isoformat(), "new_rate": Decimal(a.new), "old_rate": Decimal(a.old)}
            rec.update(currency="USD", source="simulated", simulated=True)
            if want is None or rec["type"] in want:
                out.append(rec)
        out.sort(key=lambda r: r["ex_date"])
        return envelope(out, as_of=now)

    def calendar(self, symbol: str, now: Optional[float] = None) -> dict:
        now = self._now(now)
        ca = self.corporate_actions(symbol, now=now)
        divs = dict(ca, items=[r for r in ca["items"] if r["type"] == "cash_dividend"])
        splits = dict(ca, items=[r for r in ca["items"] if r["type"] in ("forward_split", "reverse_split")])
        return {"symbol": symbol, "earnings": self.earnings(symbol, now), "dividends": divs, "splits": splits}

    # ---- analyst ratings ------------------------------------------------------------------
    RATING_START = date(2024, 1, 1)

    def rating_changes(self, symbol: str, until: date) -> list:
        """Rating changes (seeded, about three a year) from RATING_START up to `until`, oldest first."""
        hit = self._ratings.get(symbol)
        if hit is None or hit[0] < until:
            out, state = [], "hold"
            end = date(until.year + 1, 12, 31)
            monday = self.RATING_START - timedelta(days=self.RATING_START.weekday())
            while monday <= end:
                r = rng(self.seed, symbol, "rating", monday.isoformat())
                if r.random() < 0.06:
                    new = r.choice(("buy", "sell")) if state == "hold" else "hold"
                    if state == "hold" and r.random() < 0.6:
                        new = "buy"  # more upgrades than downgrades from neutral, as is usual
                    d = monday + timedelta(days=r.randrange(5))
                    if cal.is_trading_day(d):
                        upside = {"buy": r.uniform(0.12, 0.3), "hold": r.uniform(-0.03, 0.08),
                                  "sell": r.uniform(-0.22, -0.08)}[new]
                        out.append({"id": f"rating-{symbol}-{d.isoformat()}", "date": d, "ts": ny_ts(d, time(7, 0)),
                                    "from": state, "to": new, "upside": upside})
                        state = new
                monday += timedelta(days=7)
            hit = (end, out)
            self._ratings[symbol] = hit
        return [c for c in hit[1] if c["date"] <= until]

    def reference_price(self, symbol: str, ts: float) -> Optional[Decimal]:
        """Close of the last daily bar before ts, else the current price."""
        best = None
        for r in self.feed.store.daily.get(symbol, []):
            if r[0] + 86400 <= ts:
                best = r[4]
            else:
                break
        if best is None:
            return self.price(symbol)
        return Decimal(repr(best)).quantize(CENT, ROUND_HALF_UP)

    def target(self, symbol: str, change: dict) -> Optional[Decimal]:
        ref = self.reference_price(symbol, change["ts"])
        if ref is None:
            return None
        step = Decimal(1) if ref >= 20 else Decimal("0.5")
        return (ref * Decimal(repr(1 + change["upside"])) / step).quantize(Decimal(1), ROUND_HALF_UP) * step

    def current_rating(self, symbol: str, now: float) -> Optional[dict]:
        changes = [c for c in self.rating_changes(symbol, cal.ny_date(now)) if c["ts"] <= now]
        if not changes:
            return {"rating": "hold", "since": None, "target": None}
        c = changes[-1]
        return {"rating": c["to"], "since": c["date"].isoformat(), "target": self.target(symbol, c)}

    # ---- economic calendar and news ----------------------------------------------------------
    def economic_calendar(self, start: date, end: date, now: Optional[float] = None) -> dict:
        now = self._now(now)
        s_ts = ny_ts(start, time(0, 0)) - 1
        e_ts = ny_ts(end + timedelta(days=1), time(0, 0)) - 1
        rows = [macro.public(e, now, self.moves_applied(e["ts"])) for e in macro.releases(self.seed, s_ts, e_ts)]
        return envelope(rows, as_of=now)

    def news(self, symbol: str, limit: int = 20, now: Optional[float] = None) -> dict:
        from . import news
        now = self._now(now)
        return envelope(news.symbol_news(self, symbol, now, limit), as_of=now)

    def market_news(self, limit: int = 30, category: Optional[str] = None, now: Optional[float] = None) -> dict:
        from . import news
        now = self._now(now)
        return envelope(news.market_news(self, now, limit, category), as_of=now)

    def sources(self) -> dict:
        return {"news": SOURCE, "corporate_actions": SOURCE, "earnings": SOURCE, "fundamentals": SOURCE,
                "statements": SOURCE, "economic_calendar": SOURCE, "market_news": SOURCE, "reason": None,
                "simulated": True, "note": NOTE}


def seed_from_source(text: str, fallback: str) -> int:
    m = re.search(r"seed\s+(-?\d+)", text or "")
    if m:
        return int(m.group(1))
    return int.from_bytes(fallback.encode("utf-8")[:6].ljust(6, b"\0"), "big")


def for_replay(feed) -> Optional[SimResearch]:
    """Simulated research for a replay of synthetic data (fictional symbols only), else None."""
    from ..synthetic import UNIVERSE
    known = {u[0] for u in UNIVERSE}
    if not feed.simulated or not feed.info or not set(feed.info) <= known:
        return None
    return SimResearch(feed, seed_from_source(getattr(feed, "source", ""), getattr(feed, "source", "") or "replay"))
