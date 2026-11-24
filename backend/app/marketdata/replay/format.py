"""PaperDesk session files: one recorded trading session, gzip-compressed JSON lines.

See docs/REPLAY.md for the full description. In short, each line is one JSON value:

    line 1          header object   {"format": "paperdesk-session", "version": 1, ...}
    context lines   objects         {"daily": "NVLX", "bars": [[ts, o, h, l, c, v], ...]}
                                    {"minutes": "NVLX", "bars": [...]}
    event lines     arrays          ["Q", ts, "NVLX", bid, ask, bid_size, ask_size]
                                    ["T", ts, "NVLX", price, size]
    last line       trailer object  {"end": {"events": n, "end": ts}}

Prices and sizes in events are JSON numbers written from Decimal without exponent and read back
with parse_float=Decimal, so they round-trip exactly. Header prices are decimal strings.
"""
import gzip
import io
import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import IO, Iterator, Optional

FORMAT = "paperdesk-session"
VERSION = 1
SUFFIX = ".pdsession.gz"
QUOTE, TRADE = "Q", "T"
_SYMBOL = re.compile(r"^[A-Z0-9.\-]{1,12}$")
HEADER_DECIMALS = ("bid", "ask", "bid_size", "ask_size", "last", "open", "high", "low", "volume", "prev_close")


class SessionFormatError(ValueError):
    pass


def num(v) -> str:
    """JSON text for a number: Decimals without exponent, whole floats as integers."""
    if isinstance(v, Decimal):
        s = format(v, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".") or "0"
        return s
    if isinstance(v, bool):
        raise TypeError("booleans are not numbers")
    if isinstance(v, int):
        return str(v)
    f = float(v)
    if f.is_integer():
        return str(int(f))
    return repr(round(f, 3))


def session_id(path: str) -> str:
    name = os.path.basename(path)
    return name[: -len(SUFFIX)] if name.endswith(SUFFIX) else os.path.splitext(name)[0]


class SessionWriter:
    """Streams a session file. Use start(), then context(), then events, then close()."""

    def __init__(self, path_or_file):
        if isinstance(path_or_file, (str, os.PathLike)):
            d = os.path.dirname(os.fspath(path_or_file))
            if d:
                os.makedirs(d, exist_ok=True)
            self._raw: IO[bytes] = open(path_or_file, "wb")
            self._own = True
        else:
            self._raw, self._own = path_or_file, False
        # mtime=0 and no file name keep the output byte-identical for identical input.
        self._gz = gzip.GzipFile(fileobj=self._raw, mode="wb", mtime=0, filename="", compresslevel=9)
        self._text = io.TextIOWrapper(self._gz, encoding="utf-8", newline="\n")
        self.events = 0
        self.last_ts: Optional[float] = None
        self._state = "new"

    def _line(self, text: str) -> None:
        self._text.write(text)
        self._text.write("\n")

    def start(self, header: dict) -> None:
        if self._state != "new":
            raise SessionFormatError("header already written")
        h = {"format": FORMAT, "version": VERSION, **header}
        validate_header(h)
        self._line(json.dumps(h, separators=(",", ":"), sort_keys=False))
        self._state = "context"

    def context(self, kind: str, symbol: str, bars: list) -> None:
        if self._state != "context":
            raise SessionFormatError("context lines must come before the first event")
        if kind not in ("daily", "minutes"):
            raise SessionFormatError(f"unknown context kind {kind}")
        rows = ",".join("[" + ",".join(num(v) for v in r) + "]" for r in bars)
        self._line(f'{{"{kind}":"{symbol}","bars":[{rows}]}}')

    def _check_ts(self, ts: float, symbol: str) -> None:
        if self._state not in ("context", "events"):
            raise SessionFormatError("write the header first")
        if not _SYMBOL.match(symbol):
            raise SessionFormatError(f"bad symbol {symbol!r}")
        if self.last_ts is not None and ts < self.last_ts:
            raise SessionFormatError(f"events must be in time order ({ts} is before {self.last_ts})")
        self.last_ts = ts
        self._state = "events"
        self.events += 1

    def quote(self, ts: float, symbol: str, bid, ask, bid_size, ask_size) -> None:
        self._check_ts(ts, symbol)
        self._line(f'["Q",{num(ts)},"{symbol}",{num(bid)},{num(ask)},{num(bid_size)},{num(ask_size)}]')

    def trade(self, ts: float, symbol: str, price, size) -> None:
        self._check_ts(ts, symbol)
        self._line(f'["T",{num(ts)},"{symbol}",{num(price)},{num(size)}]')

    def close(self, end: Optional[float] = None) -> None:
        if self._state == "closed":
            return
        end = end if end is not None else self.last_ts
        self._line(json.dumps({"end": {"events": self.events, "end": end}}, separators=(",", ":")))
        self._text.flush()
        self._text.detach()
        self._gz.close()
        if self._own:
            self._raw.close()
        self._state = "closed"


def validate_header(h: dict) -> None:
    if h.get("format") != FORMAT:
        raise SessionFormatError("not a PaperDesk session file")
    v = h.get("version")
    if not isinstance(v, int) or v < 1:
        raise SessionFormatError("the session file has no valid version")
    if v > VERSION:
        raise SessionFormatError(f"the session file is version {v}; this program reads up to version {VERSION}")
    for k in ("date", "start", "symbols", "initial"):
        if k not in h:
            raise SessionFormatError(f"the session header is missing '{k}'")
    syms = [s.get("symbol") for s in h["symbols"]]
    if not syms or any(not isinstance(s, str) or not _SYMBOL.match(s) for s in syms):
        raise SessionFormatError("the session header has no valid symbol list")
    missing = [s for s in syms if s not in h["initial"]]
    if missing:
        raise SessionFormatError(f"the session header has no starting quote for {', '.join(missing)}")


@dataclass
class SessionData:
    header: dict
    daily: dict = field(default_factory=dict)
    minutes: dict = field(default_factory=dict)
    events: list = field(default_factory=list)  # tuples: (ts, "Q", sym, bid, ask, bid_size, ask_size) / (ts, "T", sym, price, size)
    end: float = 0.0

    @property
    def start(self) -> float:
        return float(self.header["start"])

    @property
    def symbols(self) -> list:
        return [s["symbol"] for s in self.header["symbols"]]

    def initial(self, symbol: str) -> dict:
        raw = self.header["initial"][symbol]
        return {k: (Decimal(str(raw[k])) if raw.get(k) is not None else None) for k in HEADER_DECIMALS if k in raw}


def _open(path) -> IO[str]:
    return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8")


def _loads(line: str):
    return json.loads(line, parse_float=Decimal)


def read_header(path) -> dict:
    """Read only the first line. Cheap, used to list sessions."""
    try:
        with _open(path) as f:
            line = f.readline()
        h = json.loads(line)
    except (OSError, EOFError, ValueError) as e:
        raise SessionFormatError(f"cannot read {os.path.basename(os.fspath(path))}: {e}") from None
    if not isinstance(h, dict):
        raise SessionFormatError("not a PaperDesk session file")
    validate_header(h)
    return h


def iter_records(path) -> Iterator:
    """Yield every line after the header as parsed JSON (dicts for context/trailer, lists for events)."""
    with _open(path) as f:
        f.readline()
        for line in f:
            if line.strip():
                yield _loads(line)


def read_session(path) -> SessionData:
    header = read_header(path)
    known = set(s["symbol"] for s in header["symbols"])
    data = SessionData(header=header)
    trailer = None
    last = float(header["start"])
    try:
        for rec in iter_records(path):
            if trailer is not None:
                raise SessionFormatError("data after the end marker")
            if isinstance(rec, list):
                kind, ts, sym = rec[0], float(rec[1]), rec[2]
                if sym not in known:
                    raise SessionFormatError(f"event for unknown symbol {sym}")
                if ts < last:
                    raise SessionFormatError("events are not in time order")
                last = ts
                if kind == QUOTE and len(rec) == 7:
                    data.events.append((ts, QUOTE, sym, Decimal(rec[3]), Decimal(rec[4]), Decimal(rec[5]), Decimal(rec[6])))
                elif kind == TRADE and len(rec) == 5:
                    data.events.append((ts, TRADE, sym, Decimal(rec[3]), Decimal(rec[4])))
                else:
                    raise SessionFormatError(f"unknown event {rec[:3]}")
            elif "daily" in rec:
                data.daily[rec["daily"]] = [[int(r[0]), *(float(v) for v in r[1:6])] for r in rec["bars"]]
            elif "minutes" in rec:
                data.minutes[rec["minutes"]] = [[int(r[0]), *(float(v) for v in r[1:6])] for r in rec["bars"]]
            elif "end" in rec:
                trailer = rec["end"]
            # unknown object lines are ignored so later minor versions can add context
    except (OSError, EOFError) as e:
        raise SessionFormatError(f"the session file is damaged: {e}") from None
    if trailer is None:
        raise SessionFormatError("the session file is incomplete (no end marker); it may have been cut off")
    if int(trailer.get("events", -1)) != len(data.events):
        raise SessionFormatError("the session file's event count does not match its end marker")
    end = trailer.get("end") or header.get("end") or last
    data.end = max(float(end), last, data.start)
    return data
