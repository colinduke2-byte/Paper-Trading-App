"""Build a replay session for a past trading day from a data provider's historical 1-minute bars.

    python -m app.marketdata.replay fetch --date 2026-03-13 --symbols SPY,AAPL --out day

Works on Alpaca's free plan: historical bars are free, and consolidated (SIP) history is free when the
requested end is at least 15 minutes old (docs/DATA_PROVIDERS.md), which is always true for a past
day. If SIP is refused, IEX bars are used and the session says so.

The session is labelled with where the data came from and how it was made:
- `source` names the provider, the feed (SIP or IEX) and the price basis.
- `description` states the approximation: the session is built from 1-minute bars by the
  builder in builder.py (four prints per bar, quotes one spread wide; docs/REPLAY.md). It is
  not tick data, so fills inside a minute are approximate.
- Prices of the day are as traded that day (`adjustment=raw`). The earlier daily bars used for
  chart context are fetched split-adjusted and rescaled to the same basis (by the ratio of the raw
  and the adjusted close of the previous session), so a split between then and now shows no jump
  and the chart joins the day's prices. Dividends are not adjusted for.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from ...core import calendar as cal
from ..http import ProviderError, parse_ts
from .builder import build_from_bars

DAILY_DAYS = 260


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _day_ts(ts: float) -> int:
    """A daily bar time -> UTC midnight of its New York date (the chart and builder convention)."""
    return int((cal.ny_date(ts) - date(1970, 1, 1)).total_seconds())


def _rows(raw: list, daily: bool) -> list:
    out = {}
    for r in raw or []:
        t = parse_ts(r.get("t"))
        if t is None:
            continue
        k = _day_ts(t) if daily else int(t)
        out[k] = [k, *(Decimal(str(r[x])) for x in ("o", "h", "l", "c")), int(Decimal(str(r.get("v", 0))))]
    return [out[k] for k in sorted(out)]


def _bars(rest, symbol, tf, start, end, feed, adjustment):
    """(rows, feed used). SIP falls back to IEX when the plan refuses it."""
    try:
        return rest.bars_range(symbol, tf, start, end, feed, adjustment, max_wait=30), feed
    except ProviderError as e:
        if feed == "sip" and e.status in (400, 403, 422):
            return rest.bars_range(symbol, tf, start, end, "iex", adjustment, max_wait=30), "iex"
        raise


def build_from_provider(rest, day: date, symbols: list, out, feed: str = "sip", names: Optional[dict] = None,
                        spread_bps: float = 2.0, label: str = "", provider: str = "Alpaca") -> dict:
    """Fetch the regular session of `day` for `symbols` and write a session file. Returns the builder's stats
    plus `feeds` (the feed each symbol came from) and `skipped` (symbols with no bars that day)."""
    if not cal.is_trading_day(day):
        raise ValueError(f"{day.isoformat()} is not a US trading day")
    open_ts = datetime.combine(day, cal.OPEN, tzinfo=cal.NY).timestamp()
    close_ts = cal.close_ts(day)
    names = names or {}
    minutes, daily, meta, feeds, skipped = {}, {}, {}, {}, []
    for sym in dict.fromkeys(s.strip().upper() for s in symbols if s.strip()):
        raw, used = _bars(rest, sym, "1Min", _iso(open_ts), _iso(close_ts - 1), feed, "raw")
        rows = [r for r in _rows(raw, False) if open_ts <= r[0] < close_ts]
        if not rows:
            skipped.append(sym)
            continue
        minutes[sym], feeds[sym] = rows, used
        start = (day - timedelta(days=int(DAILY_DAYS * 1.5) + 10)).isoformat()
        end = (day - timedelta(days=1)).isoformat()
        adj, _ = _bars(rest, sym, "1Day", start, end, used, "split")
        unadj, _ = _bars(rest, sym, "1Day", (day - timedelta(days=10)).isoformat(), end, used, "raw")
        adj, unadj = _rows(adj, True), _rows(unadj, True)
        if adj and unadj and adj[-1][0] == unadj[-1][0] and adj[-1][4] > 0:
            f = unadj[-1][4] / adj[-1][4]  # 1 unless a split happened after `day`
            adj = [[r[0], *(v * f for v in r[1:5]), int(r[5] / f) if f else r[5]] for r in adj]
        daily[sym] = adj[-DAILY_DAYS:]
        meta[sym] = {"name": names.get(sym, sym), "sector": "", "type": "stock"}
    if not minutes:
        raise ValueError(f"{provider} returned no 1-minute bars for {', '.join(skipped) or 'these symbols'} "
                         f"on {day.isoformat()}")
    used_feeds = sorted(set(feeds.values()))
    feed_text = " and ".join(f.upper() for f in used_feeds)
    source = (f"{provider} historical 1-minute bars, {feed_text} feed; prices as traded on {day.isoformat()}, "
              f"earlier daily bars split-adjusted to that basis")
    description = ("Built from 1-minute bars, not tick data: within each minute the price path (open, then the "
                   "nearer extreme, the other extreme, then close) and the quotes around it are approximated, so "
                   "fills inside a minute are approximate. See docs/REPLAY.md." +
                   (" IEX bars hold only the IEX exchange's trades (about 2.5% of US volume)." if "iex" in used_feeds
                    else ""))
    stats = build_from_bars(out, minutes, daily, meta, label=label or f"{day.isoformat()} {', '.join(sorted(minutes))} "
                            f"({provider} 1-minute bars)", description=description, source=source, simulated=False,
                            spread_bps=spread_bps, start=open_ts, end=close_ts)
    stats.update(feeds=feeds, skipped=skipped)
    return stats
