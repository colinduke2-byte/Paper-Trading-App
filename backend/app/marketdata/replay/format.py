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

from ...core import limits

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
    if not isinstance(h["date"], str) or len(h["date"]) > 32:
        raise SessionFormatError("the session header has no valid date")
    if not _finite(h["start"]) or (h.get("end") is not None and not _finite(h["end"])):
        raise SessionFormatError("the session header has no valid start or end time")
    if not isinstance(h["symbols"], list) or not 0 < len(h["symbols"]) <= limits.MAX_SESSION_SYMBOLS \
            or not all(isinstance(s, dict) for s in h["symbols"]):
        raise SessionFormatError("the session header has no valid symbol list")
    syms = [s.get("symbol") for s in h["symbols"]]
    if any(not isinstance(s, str) or not _SYMBOL.match(s) for s in syms):
        raise SessionFormatError("the session header has no valid symbol list")
    if not isinstance(h["initial"], dict):
        raise SessionFormatError("the session header has no valid starting quotes")
    missing = [s for s in syms if s not in h["initial"]]
    if missing:
        raise SessionFormatError(f"the session header has no starting quote for {', '.join(missing[:5])}")
    for sym in syms:
        raw = h["initial"][sym]
        if not isinstance(raw, dict):
            raise SessionFormatError(f"the starting quote for {sym} is not valid")
        for k in HEADER_DECIMALS:
            if raw.get(k) is not None and _number(raw[k], positive=False) is None:
                raise SessionFormatError(f"the starting {k.replace('_', ' ')} for {sym} is not a finite number")
    for k in ("label", "description", "source"):
        if h.get(k) is not None and not isinstance(h[k], str):
            raise SessionFormatError(f"the session header's {k} is not text")


_MAX_VALUE = Decimal("1e12")


def _finite(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float, Decimal)) and Decimal(str(v)).is_finite()


def _number(v, positive: bool) -> Optional[Decimal]:
    """A finite, non-negative (or positive) number not above 1e12 from a session file, else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal, str)) or (isinstance(v, str) and len(v) > 40):
        return None
    try:
        d = Decimal(str(v))
    except ArithmeticError:
        return None
    if not d.is_finite() or d < 0 or (positive and d == 0) or d > _MAX_VALUE:
        return None
    return d


def _context_symbol(sym, known: set) -> str:
    if not isinstance(sym, str) or sym not in known:
        raise SessionFormatError(f"context bars for unknown symbol {str(sym)[:20]}")
    return sym


def _bars(rows) -> list:
    """[[ts, open, high, low, close, volume], ...] with finite numbers only."""
    if not isinstance(rows, list):
        raise SessionFormatError("context bars are not a list")
    out = []
    for r in rows:
        if not isinstance(r, list) or len(r) < 6 or not all(_finite(v) for v in r[:6]):
            raise SessionFormatError("a context bar is not six finite numbers")
        out.append([int(r[0]), *(float(v) for v in r[1:6])])
    return out


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


def _no_constant(name):
    raise ValueError(f"{name} is not a number")


def _loads(line: str):
    return json.loads(line, parse_float=Decimal, parse_constant=_no_constant)


def _line(f, limit: int) -> str:
    """One line, read with a bound: a damaged or crafted file cannot make a reader hold a huge line."""
    line = f.readline(limit + 1)
    if len(line) > limit:
        raise SessionFormatError(f"a line in the session file is longer than {limit // limits.MB} MB")
    return line


def read_header(path) -> dict:
    """Read only the first line. Cheap, used to list sessions."""
    try:
        with _open(path) as f:
            line = _line(f, limits.MAX_SESSION_HEADER)
        h = json.loads(line, parse_constant=_no_constant)
    except (OSError, EOFError, ValueError, UnicodeDecodeError, RecursionError) as e:
        raise SessionFormatError(f"cannot read {os.path.basename(os.fspath(path))}: {e}") from None
    if not isinstance(h, dict):
        raise SessionFormatError("not a PaperDesk session file")
    validate_header(h)
    return h


def iter_records(path) -> Iterator:
    """Yield every line after the header as parsed JSON (dicts for context/trailer, lists for events)."""
    with _open(path) as f:
        _line(f, limits.MAX_SESSION_HEADER)
        while True:
            line = _line(f, limits.MAX_SESSION_LINE)
            if not line:
                return
            if line.strip():
                try:
                    yield _loads(line)
                except (ValueError, RecursionError) as e:
                    raise SessionFormatError(f"the session file has a line that is not valid JSON: {str(e)[:80]}") from None


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
                if len(rec) < 3 or not _finite(rec[1]) or not isinstance(rec[2], str):
                    raise SessionFormatError(f"bad event {str(rec[:3])[:80]}")
                kind, ts, sym = rec[0], float(rec[1]), rec[2]
                if sym not in known:
                    raise SessionFormatError(f"event for unknown symbol {sym[:20]}")
                if ts < last:
                    raise SessionFormatError("events are not in time order")
                last = ts
                if kind == QUOTE and len(rec) == 7:
                    vals = [_number(rec[3], False), _number(rec[4], True), _number(rec[5], False), _number(rec[6], False)]  # a zero bid is "no bid"
                    if any(v is None for v in vals):
                        raise SessionFormatError(f"a quote for {sym} at {ts:.0f} has a price or size that is not a valid number")
                    data.events.append((ts, QUOTE, sym, *vals))
                elif kind == TRADE and len(rec) == 5:
                    vals = [_number(rec[3], True), _number(rec[4], False)]
                    if any(v is None for v in vals):
                        raise SessionFormatError(f"a trade for {sym} at {ts:.0f} has a price or size that is not a valid number")
                    data.events.append((ts, TRADE, sym, *vals))
                else:
                    raise SessionFormatError(f"unknown event {str(rec[:3])[:80]}")
            elif not isinstance(rec, dict):
                raise SessionFormatError("the session file has a line that is neither an event nor an object")
            elif "daily" in rec:
                data.daily[_context_symbol(rec["daily"], known)] = _bars(rec.get("bars"))
            elif "minutes" in rec:
                data.minutes[_context_symbol(rec["minutes"], known)] = _bars(rec.get("bars"))
            elif "end" in rec:
                if not isinstance(rec["end"], dict):
                    raise SessionFormatError("the session file's end marker is not valid")
                trailer = rec["end"]
            # unknown object lines are ignored so later minor versions can add context
    except (OSError, EOFError) as e:
        raise SessionFormatError(f"the session file is damaged: {e}") from None
    if trailer is None:
        raise SessionFormatError("the session file is incomplete (no end marker); it may have been cut off")
    if not isinstance(trailer.get("events", -1), int) or (trailer.get("end") is not None and not _finite(trailer["end"])):
        raise SessionFormatError("the session file's end marker is not valid")
    if int(trailer.get("events", -1)) != len(data.events):
        raise SessionFormatError("the session file's event count does not match its end marker")
    end = trailer.get("end") or header.get("end") or last
    data.end = max(float(end), last, data.start)
    return data
