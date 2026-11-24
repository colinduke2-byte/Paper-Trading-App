"""Watchlist layouts, saved screens and recent symbols, saved in <data dir>/discovery.json.

The symbol lists themselves stay in Exchange.watchlists (state.json), in display order, so an old
state file with only {"Main": [...]} loads unchanged: a list without a saved layout gets the
default columns. This file only adds what the older format did not have.
"""
import json
import threading
import uuid
from pathlib import Path
from typing import Optional

COLUMNS = {
    "last": "Last", "change": "Change", "change_pct": "Change %", "bid": "Bid", "ask": "Ask", "volume": "Volume",
    "day_range": "Day range", "range_52w": "52-week range", "market_cap": "Market cap", "pe": "P/E",
    "dividend_yield": "Dividend yield", "position_qty": "Position", "unrealized_pnl": "Unrealised gain",
    "sparkline": "Today",
}
DEFAULT_COLUMNS = ["last", "change", "change_pct"]
DENSITIES = ("compact", "comfortable")
SORT_KEYS = {"symbol", "name", "last", "change", "change_pct", "bid", "ask", "volume", "day_range", "range_52w",
             "market_cap", "pe", "dividend_yield", "position_qty", "unrealized_pnl"}
MAX_LISTS = 50
MAX_SYMBOLS = 100
MAX_SCREENS = 100
MAX_RECENT = 12
NAME_MAX = 30


def default_layout() -> dict:
    return {"columns": list(DEFAULT_COLUMNS), "density": "comfortable", "sort": None}


def check_name(name, what: str = "Watchlist") -> str:
    name = str(name if name is not None else "").strip()
    if not name:
        raise ValueError(f"{what} name cannot be empty")
    if len(name) > NAME_MAX:
        raise ValueError(f"{what} name must be at most {NAME_MAX} characters")
    if any(c in name for c in "/\\") or any(ord(c) < 32 for c in name):
        raise ValueError(f"{what} name cannot contain slashes or control characters")
    return name


def check_layout(body: dict, base: Optional[dict] = None) -> dict:
    """Validate a (partial) layout; returns the merged layout."""
    out = dict(base or default_layout())
    if not isinstance(body, dict):
        raise ValueError("layout must be an object")
    unknown = set(body) - {"columns", "density", "sort"}
    if unknown:
        raise ValueError(f"unknown layout field {sorted(unknown)[0]}")
    if "columns" in body:
        cols = body["columns"]
        if not isinstance(cols, list) or not all(isinstance(c, str) for c in cols):
            raise ValueError("columns must be a list of column names")
        bad = [c for c in cols if c not in COLUMNS]
        if bad:
            raise ValueError(f"unknown column {bad[0]}; choose from {', '.join(COLUMNS)}")
        out["columns"] = list(dict.fromkeys(cols))
    if "density" in body:
        if body["density"] not in DENSITIES:
            raise ValueError("density must be compact or comfortable")
        out["density"] = body["density"]
    if "sort" in body:
        s = body["sort"]
        if s is None:
            out["sort"] = None
        else:
            if not isinstance(s, dict) or s.get("key") not in SORT_KEYS:
                raise ValueError(f"sort key must be one of {', '.join(sorted(SORT_KEYS))}")
            out["sort"] = {"key": s["key"], "desc": bool(s.get("desc", False))}
    return out


class DiscoveryStore:
    def __init__(self, data_dir: Optional[str]):
        self.path = Path(data_dir) / "discovery.json" if data_dir else None
        self.lock = threading.RLock()
        self.layouts: dict = {}
        self.screens: list = []
        self.recent: list = []
        self.load()

    def load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # a damaged file must not stop the app; defaults are used and it is rewritten on the next change
        for name, lay in (data.get("layouts") or {}).items():
            try:
                self.layouts[name] = check_layout(lay)
            except ValueError:
                self.layouts[name] = default_layout()
        self.screens = [s for s in data.get("screens") or [] if isinstance(s, dict) and s.get("id")]
        self.recent = [str(s) for s in data.get("recent") or []][:MAX_RECENT]

    def save(self) -> None:
        if not self.path:
            return
        from ..exchange import Exchange
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {"version": 1, "layouts": self.layouts, "screens": self.screens, "recent": self.recent}
        Exchange._write(str(self.path), data)  # atomic replace, with the Windows retry

    # ---- layouts ---------------------------------------------------------------------------
    def layout(self, name: str) -> dict:
        return dict(self.layouts.get(name) or default_layout())

    def set_layout(self, name: str, layout: dict) -> None:
        with self.lock:
            self.layouts[name] = layout
        self.save()

    def rename(self, old: str, new: str) -> None:
        with self.lock:
            if old in self.layouts:
                self.layouts[new] = self.layouts.pop(old)
        self.save()

    def forget(self, name: str) -> None:
        with self.lock:
            self.layouts.pop(name, None)
        self.save()

    # ---- screens ----------------------------------------------------------------------------
    def add_screen(self, screen: dict) -> dict:
        with self.lock:
            if len(self.screens) >= MAX_SCREENS:
                raise ValueError(f"at most {MAX_SCREENS} saved screens; delete one first")
            if any(s["name"].lower() == screen["name"].lower() for s in self.screens):
                raise ValueError(f"a saved screen called {screen['name']} already exists")
            screen = dict(screen, id=uuid.uuid4().hex[:12])
            self.screens.append(screen)
        self.save()
        return screen

    def update_screen(self, sid: str, screen: dict) -> dict:
        with self.lock:
            for i, s in enumerate(self.screens):
                if s["id"] == sid:
                    if any(o["id"] != sid and o["name"].lower() == screen["name"].lower() for o in self.screens):
                        raise ValueError(f"a saved screen called {screen['name']} already exists")
                    self.screens[i] = dict(screen, id=sid)
                    break
            else:
                raise KeyError(f"no saved screen with id {sid}")
        self.save()
        return self.screens[i]

    def delete_screen(self, sid: str) -> None:
        with self.lock:
            before = len(self.screens)
            self.screens = [s for s in self.screens if s["id"] != sid]
            if len(self.screens) == before:
                raise KeyError(f"no saved screen with id {sid}")
        self.save()

    # ---- recent symbols -----------------------------------------------------------------------
    def touch_recent(self, symbol: str) -> list:
        with self.lock:
            self.recent = [symbol] + [s for s in self.recent if s != symbol]
            self.recent = self.recent[:MAX_RECENT]
        self.save()
        return list(self.recent)
