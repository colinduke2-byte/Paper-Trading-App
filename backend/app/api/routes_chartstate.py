"""Chart settings and drawings, saved with the app state so they survive a browser change.

The chart stores opaque JSON records of the form {"updated": <client ms>, "data": <anything>}; the server checks
only their shape and size. A record is replaced only by one with an equal or newer `updated` stamp, so a stale tab
cannot overwrite newer work.

Scopes. Chart settings (chart type, indicators, scale) are one per user. Drawings are kept per scope, because live
trading and each recorded replay session are different price series: a line drawn on a replay must not appear on
the live chart. The scope is given by the client (`?scope=live` or `?scope=replay:<session id>`) rather than read
from the active exchange, so a save sent just before a live/replay switch still lands where it was drawn.

Storage: chartstate.json in the LIVE exchange's data directory (ctx.live), never in the replay sandbox, which is
wiped when a replay starts. With no data directory (unit tests) the state is kept in memory.
"""
import json
import os
import re
import threading
from typing import Optional

from fastapi import Body, HTTPException, Query

MAX_BYTES = 256 * 1024
MAX_SYMBOLS = 500
MAX_SCOPES = 50
SYMBOL = re.compile(r"^[A-Z0-9.\-]{1,24}$")
SCOPE = re.compile(r"^(live|replay:[A-Za-z0-9._\-]{1,80})$")


def register(app, ctx):
    lock = threading.Lock()
    cache: dict = {}  # data_dir (or None) -> document

    def live():
        return getattr(ctx, "live", None) or ctx.ex

    def path():
        d = getattr(live(), "data_dir", None)
        return os.path.join(d, "chartstate.json") if d else None

    def doc() -> dict:
        key = path()
        if key not in cache:
            data = {}
            if key:
                try:
                    with open(key, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    data = {}
            if not isinstance(data, dict):
                data = {}
            scopes = data.get("scopes")
            if not isinstance(scopes, dict):
                scopes = {}
            # Version 1 files kept live drawings at the top level.
            if isinstance(data.get("drawings"), dict) and "live" not in scopes:
                scopes["live"] = data["drawings"]
            cache[key] = {"version": 2, "settings": data.get("settings"), "scopes": scopes}
        return cache[key]

    def save(data: dict) -> None:
        p = path()
        if p:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            type(live())._write(p, data)

    def record(body) -> dict:
        if not isinstance(body, dict) or "data" not in body:
            raise HTTPException(400, "body must be an object with 'updated' and 'data'")
        updated = body.get("updated")
        if isinstance(updated, bool) or not isinstance(updated, (int, float)) or updated != updated or updated < 0:
            raise HTTPException(400, "'updated' must be a non-negative number")
        if len(json.dumps(body["data"])) > MAX_BYTES:
            raise HTTPException(413, f"chart state is larger than {MAX_BYTES // 1024} KB")
        return {"updated": updated, "data": body["data"]}

    def put(current, rec, store):
        if current is not None and current.get("updated", 0) > rec["updated"]:
            return current  # keep the newer record
        store(rec)
        return rec

    def symbol(s: str) -> str:
        s = s.upper()
        if not SYMBOL.match(s):
            raise HTTPException(400, "invalid symbol")
        return s

    def scope(s: Optional[str]) -> str:
        s = s or "live"
        if not SCOPE.match(s):
            raise HTTPException(400, "scope must be 'live' or 'replay:<session id>'")
        return s

    @app.get("/api/chartstate")
    def get_state(scope_: Optional[str] = Query(None, alias="scope")):
        sc = scope(scope_)
        with lock:
            d = doc()
            return {"scope": sc, "settings": d["settings"], "drawings": d["scopes"].get(sc, {})}

    @app.put("/api/chartstate/settings")
    def put_settings(body: dict = Body(...)):
        rec = record(body)
        with lock:
            d = doc()
            out = put(d["settings"], rec, lambda r: d.__setitem__("settings", r))
            save(d)
            return out

    @app.get("/api/chartstate/drawings/{sym}")
    def get_drawings(sym: str, scope_: Optional[str] = Query(None, alias="scope")):
        sc = scope(scope_)
        with lock:
            return doc()["scopes"].get(sc, {}).get(symbol(sym))

    @app.put("/api/chartstate/drawings/{sym}")
    def put_drawings(sym: str, body: dict = Body(...), scope_: Optional[str] = Query(None, alias="scope")):
        s, sc, rec = symbol(sym), scope(scope_), record(body)
        if not isinstance(rec["data"], list):
            raise HTTPException(400, "drawings must be a list")
        with lock:
            d = doc()
            if sc not in d["scopes"] and len(d["scopes"]) >= MAX_SCOPES:
                raise HTTPException(413, f"drawings are saved for at most {MAX_SCOPES} sessions")
            drawings = d["scopes"].setdefault(sc, {})
            if s not in drawings and len(drawings) >= MAX_SYMBOLS:
                raise HTTPException(413, f"drawings are saved for at most {MAX_SYMBOLS} symbols")
            out = put(drawings.get(s), rec, lambda r: drawings.__setitem__(s, r))
            save(d)
            return out

    @app.delete("/api/chartstate")
    def reset_state():
        with lock:
            d = doc()
            d["settings"], d["scopes"] = None, {}
            save(d)
            return {"settings": None, "scopes": {}}
