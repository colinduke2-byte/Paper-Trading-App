"""Build a session file from one-minute bars.

Bars only say where the price went within each minute, not how, so the quote and trade stream is an
approximation (documented in docs/REPLAY.md):

* Each bar becomes four prints at :00, :15, :30 and :45 seconds: open, then the extreme nearer the
  open's side (low first on an up bar, high first on a down bar), then the other extreme, then close.
* The bar's volume is split evenly over the four prints (whole shares, remainder on the close print).
  A bar with no volume gives one quote update and no prints.
* The quote around each print is one spread wide (`spread_bps` of the price, at least one cent): a
  print at or above the previous print trades at the ask, a lower print trades at the bid.
* Displayed size on each side is a twentieth of the bar's volume rounded down to a round lot (at least
  100 shares).

Nothing is random, so the same bars always give the same file.
"""
import csv
import json
import math
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from ...core import calendar as cal
from ...core.money import CENT
from .format import SessionWriter

OFFSETS = (0, 15, 30, 45)


def _cents(x) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_time(text: str, daily: bool = False) -> int:
    """Epoch seconds from epoch digits or ISO 8601. Times without an offset are New York time."""
    text = text.strip()
    if text.replace(".", "", 1).isdigit():
        return int(float(text))
    if daily and len(text) == 10:
        return int((date.fromisoformat(text) - date(1970, 1, 1)).total_seconds())
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=cal.NY)
    if daily:
        return int((dt.date() - date(1970, 1, 1)).total_seconds())
    return int(dt.timestamp())


def read_bars_csv(path, daily: bool = False) -> dict:
    """CSV with a header row: symbol,time,open,high,low,close,volume. Returns {symbol: [[ts,o,h,l,c,v]...]}."""
    out: dict = {}
    with open(path, encoding="utf-8", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            try:
                sym = row["symbol"].strip().upper()
                bar = [parse_time(row["time"], daily), *(_cents(row[k]) for k in ("open", "high", "low", "close")),
                       int(Decimal(row["volume"]))]
            except (KeyError, ValueError, ArithmeticError) as e:
                raise ValueError(f"{path} line {i}: cannot read the bar ({e})") from None
            out.setdefault(sym, []).append(bar)
    for rows in out.values():
        rows.sort(key=lambda r: r[0])
    return out


def _estimate_vol(daily_rows: list, minute_rows: list) -> float:
    closes = [float(r[4]) for r in daily_rows[-60:]]
    if len(closes) >= 20:
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        per = 252
    else:
        closes = [float(r[4]) for r in minute_rows]
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        per = 252 * 390
    if len(rets) < 5:
        return 0.3
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    return round(min(max(sd * math.sqrt(per), 0.05), 3.0), 4)


def _lot(volume: int) -> Decimal:
    return Decimal(max(100, (volume // 20) // 100 * 100))


def build_from_bars(path, minutes: dict, daily: Optional[dict] = None, meta: Optional[dict] = None,
                    label: str = "", description: str = "", source: str = "bars", simulated: bool = False,
                    spread_bps: float = 2.0, start: Optional[float] = None, end: Optional[float] = None) -> dict:
    """Write a session from {symbol: one-minute bars}. All bars must belong to one New York trading date."""
    daily, meta = daily or {}, meta or {}
    symbols = sorted(s for s, rows in minutes.items() if rows)
    if not symbols:
        raise ValueError("no intraday bars to build a session from")
    days = {cal.ny_date(r[0]) for s in symbols for r in minutes[s]}
    if len(days) != 1:
        raise ValueError(f"the bars cover {len(days)} dates; a session is one trading date")
    day = days.pop()
    midnight = int((day - date(1970, 1, 1)).total_seconds())
    first = min(minutes[s][0][0] for s in symbols)
    last = max(minutes[s][-1][0] for s in symbols)
    start = float(start if start is not None else first)
    end = float(end if end is not None else last + 60)
    bps = Decimal(str(spread_bps)) / Decimal(10000)

    infos, initial, events = [], {}, []
    for idx, s in enumerate(symbols):
        rows = minutes[s]
        prior = [r for r in daily.get(s, []) if r[0] < midnight]
        m = dict(meta.get(s, {}))
        info = {"symbol": s, "name": m.get("name", s), "sector": m.get("sector", "Unknown"),
                "type": m.get("type", "stock"), "vol": float(m.get("vol") or _estimate_vol(prior, rows))}
        for k in ("shares_outstanding", "eps", "dividend"):
            if k in m:
                info[k] = m[k]
        infos.append(info)
        o0 = _cents(rows[0][1])
        prev_close = _cents(prior[-1][4]) if prior else o0
        sp = max(CENT, _cents(o0 * bps))
        lot0 = _lot(rows[0][5])
        initial[s] = {"bid": str(o0 - sp), "ask": str(o0), "bid_size": str(lot0), "ask_size": str(lot0),
                      "last": str(o0), "open": str(o0), "high": str(o0), "low": str(o0), "volume": "0",
                      "prev_close": str(prev_close)}
        prev_px, seq = o0, 0
        for ts, o, h, l, c, v in rows:
            o, h, l, c = _cents(o), _cents(h), _cents(l), _cents(c)
            path_px = (o, l, h, c) if c >= o else (o, h, l, c)
            v = int(v)
            lot = _lot(v)
            if v <= 0:
                sp = max(CENT, _cents(c * bps))
                events.append((ts, idx, seq, ("Q", s, c - sp, c, lot, lot)))
                seq += 1
                continue
            share = v // 4
            sizes = [share, share, share, v - 3 * share]
            for off, px, size in zip(OFFSETS, path_px, sizes):
                if size <= 0:
                    continue
                sp = max(CENT, _cents(px * bps))
                bid, ask = (px - sp, px) if px >= prev_px else (px, px + sp)
                events.append((ts + off, idx, seq, ("Q", s, max(bid, CENT), ask, lot, lot)))
                events.append((ts + off, idx, seq + 1, ("T", s, px, Decimal(size))))
                seq += 2
                prev_px = px
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    w = SessionWriter(path)
    w.start({"label": label or f"Session of {day.isoformat()}", "description": description, "date": day.isoformat(),
             "start": start, "end": end, "timezone": "America/New_York", "source": source,
             "simulated": bool(simulated), "built_from": "1-minute bars", "symbols": infos, "initial": initial})
    for s in symbols:
        prior = [[r[0], *(float(v) for v in r[1:5]), r[5]] for r in daily.get(s, []) if r[0] < midnight]
        if prior:
            w.context("daily", s, prior[-260:])
    for ts, _, _, ev in events:
        if ts < start or ts > end:
            continue
        if ev[0] == "Q":
            w.quote(ts, *ev[1:])
        else:
            w.trade(ts, *ev[1:])
    w.close(end)
    return {"path": str(path), "events": w.events, "symbols": len(symbols), "date": day.isoformat()}


def read_meta(path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("the metadata file must hold an object keyed by symbol")
    return {k.upper(): v for k, v in data.items()}
