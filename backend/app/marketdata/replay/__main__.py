"""Command line: record, build and inspect replay session files.

    python -m app.marketdata.replay record --date 2026-03-13 --seed 11 --out my.pdsession.gz
    python -m app.marketdata.replay record --realtime 600 --out live.pdsession.gz
    python -m app.marketdata.replay build --bars minutes.csv --daily daily.csv --out day.pdsession.gz
    python -m app.marketdata.replay inspect my.pdsession.gz
    python -m app.marketdata.replay samples
"""
import argparse
import importlib
import sys
import time
from collections import Counter
from datetime import date

from ...core import calendar as cal
from .format import SUFFIX, SessionFormatError, read_session


def _provider(spec: str, seed: int, now: float, always_open: bool):
    if spec == "synthetic":
        from ..synthetic import SyntheticFeed
        return SyntheticFeed(seed=seed, now=now, history_days=260, always_open=always_open)
    mod, _, cls = spec.partition(":")
    if not cls:
        raise SystemExit("--feed must be 'synthetic' or module:Class of a MarketDataProvider")
    return getattr(importlib.import_module(mod), cls)()


def _out(path: str) -> str:
    return path if path.endswith(SUFFIX) else path + SUFFIX


def cmd_record(a) -> int:
    from .recorder import realtime, record, session_times
    symbols = [s.strip().upper() for s in a.symbols.split(",")] if a.symbols else None
    if a.realtime:
        feed = _provider(a.feed, a.seed, time.time(), always_open=True)
        print(f"Recording {feed.name} for {a.realtime:g} s; press Ctrl+C to stop early.")
        try:
            stats = record(feed, _out(a.out), realtime(a.realtime, a.step), symbols=symbols, label=a.label or "",
                           source=feed.name)
        except KeyboardInterrupt:
            print("Stopped. The file is incomplete only if it has no end marker; check it with 'inspect'.")
            return 1
    else:
        day = date.fromisoformat(a.date)
        from datetime import datetime
        open_ts = datetime.combine(day, cal.OPEN, tzinfo=cal.NY).timestamp()
        feed = _provider(a.feed, a.seed, open_ts, always_open=False)
        stats = record(feed, _out(a.out), session_times(day, a.step), end=cal.close_ts(day), symbols=symbols,
                       label=a.label or "", source=f"{feed.name} feed, seed {a.seed}" if a.feed == "synthetic" else feed.name,
                       fresh_day=True)
    print(f"Wrote {stats['events']} events for {stats['symbols']} symbols to {stats['path']}")
    return 0


def cmd_build(a) -> int:
    from .builder import build_from_bars, read_bars_csv, read_meta
    minutes = read_bars_csv(a.bars)
    daily = read_bars_csv(a.daily, daily=True) if a.daily else {}
    meta = read_meta(a.meta) if a.meta else {}
    stats = build_from_bars(_out(a.out), minutes, daily, meta, label=a.label or "", source=a.source,
                            simulated=a.simulated, spread_bps=a.spread_bps)
    print(f"Wrote {stats['events']} events for {stats['symbols']} symbols ({stats['date']}) to {stats['path']}")
    return 0


def cmd_inspect(a) -> int:
    try:
        s = read_session(a.path)
    except SessionFormatError as e:
        print(f"Cannot read the session: {e}", file=sys.stderr)
        return 1
    h = s.header
    kinds = Counter(e[1] for e in s.events)
    print(f"{h.get('label')}  ({'simulated data' if h.get('simulated', True) else 'recorded market data'})")
    print(f"  date {h['date']}  {cal.ny_datetime(s.start):%H:%M:%S} to {cal.ny_datetime(s.end):%H:%M:%S} New York time")
    print(f"  source {h.get('source', '?')}  format version {h['version']}")
    print(f"  {len(s.events)} events: {kinds.get('Q', 0)} quotes, {kinds.get('T', 0)} trades")
    last = {}
    for e in s.events:
        if e[1] == "T":
            last[e[2]] = e[3]
    for sym in s.symbols:
        init = s.initial(sym)
        n = sum(1 for e in s.events if e[2] == sym)
        print(f"  {sym:6} start {init.get('last')}  end {last.get(sym, init.get('last'))}  "
              f"{n} events  {len(s.daily.get(sym, []))} daily bars")
    return 0


def cmd_samples(a) -> int:
    from .samples import build_all
    for st in build_all():
        print(f"Wrote {st['events']} events to {st['path']}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.marketdata.replay", description="Record, build and inspect replay sessions.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record a market data provider to a session file")
    r.add_argument("--out", required=True)
    r.add_argument("--feed", default="synthetic", help="'synthetic' or module:Class of a MarketDataProvider")
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--date", help="simulate this whole trading date (synthetic feed only)")
    r.add_argument("--realtime", type=float, help="record the provider live for this many seconds")
    r.add_argument("--step", type=float, default=5.0, help="seconds between snapshots")
    r.add_argument("--symbols", help="comma-separated symbols (default: all)")
    r.add_argument("--label")
    b = sub.add_parser("build", help="build a session from one-minute bars (CSV)")
    b.add_argument("--bars", required=True, help="CSV: symbol,time,open,high,low,close,volume")
    b.add_argument("--daily", help="CSV of earlier daily bars, same columns")
    b.add_argument("--meta", help="JSON object of symbol metadata (name, sector, vol...)")
    b.add_argument("--out", required=True)
    b.add_argument("--label")
    b.add_argument("--source", default="bars")
    b.add_argument("--simulated", action="store_true", help="mark the data as simulated")
    b.add_argument("--spread-bps", type=float, default=2.0)
    i = sub.add_parser("inspect", help="summarise a session file")
    i.add_argument("path")
    sub.add_parser("samples", help="regenerate the bundled sample sessions")
    a = p.parse_args(argv)
    if a.cmd == "record" and not a.date and not a.realtime:
        p.error("record needs --date (simulate a whole day) or --realtime SECONDS")
    return {"record": cmd_record, "build": cmd_build, "inspect": cmd_inspect, "samples": cmd_samples}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
