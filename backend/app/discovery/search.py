"""Symbol search by ticker or company name, ranked, over the feed's whole symbol list.

The index is rebuilt only when the feed's symbol table changes, so a query over ~11,000
symbols is one pass of string prefix checks (a few milliseconds).
"""
import re

MAX_LIMIT = 50
_WORD = re.compile(r"[A-Z0-9]+")


class SearchIndex:
    def __init__(self):
        self._key = None
        self._rows: list = []

    def _build(self, info: dict) -> None:
        key = (id(info), len(info))
        if key == self._key:
            return
        rows = []
        for sym, i in info.items():
            name = str(i.get("name") or sym)
            up = name.upper()
            rows.append((sym, up, _WORD.findall(up), name, i.get("type") or "stock", i.get("exchange") or "",
                         i.get("sector") or ""))
        self._rows, self._key = rows, key

    def search(self, info: dict, q: str, limit: int = 10, types=None) -> list:
        self._build(info)
        q = (q or "").upper().strip()
        if not q:
            return []
        terms = _WORD.findall(q)
        hits = []
        for sym, up, words, name, kind, exch, sector in self._rows:
            if types and kind not in types:
                continue
            if sym == q:
                score = 0.0
            elif sym.startswith(q):
                score = 1.0
            elif up.startswith(q):
                score = 2.0
            elif terms and all(any(w.startswith(t) for w in words) for t in terms):
                score = 2.5  # every typed word starts a word of the name ("orbit cl" -> Orbit Cloud Systems)
            elif q in sym:
                score = 3.0
            elif q in up:
                score = 4.0
            else:
                continue
            hits.append((score, len(sym), sym, name, kind, exch, sector))
        hits.sort()
        return [{"symbol": h[2], "name": h[3], "type": h[4], "exchange": h[5] or None, "sector": h[6] or None,
                 "match": ("symbol" if h[0] < 2 else "name" if h[0] < 3 else "symbol" if h[0] < 4 else "name")}
                for h in hits[:limit]]
