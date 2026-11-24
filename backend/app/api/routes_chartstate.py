"""Chart settings and drawings, saved with the app state so they survive a browser change.

The chart stores opaque JSON records of the form {"updated": <client ms>, "data": <anything>}; the server checks
only their shape and size. A record is replaced only by one with an equal or newer `updated` stamp, so a stale tab
cannot overwrite newer work. Records live in chartstate.json next to state.json in the data directory (in memory
when the exchange has no data directory, as in unit tests).
"""
import json
import os
import re
import threading

from fastapi import Body, HTTPException

MAX_BYTES = 256 * 1024
MAX_SYMBOLS = 500
SYMBOL = re.compile(r"^[A-Z0-9.\-]{1,24}$")


def register(app, ctx):
    lock = threading.Lock()
    cache: dict = {}  # data_dir (or None) -> document

    def path():
        d = getattr(ctx.ex, "data_dir", None)
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
            data.setdefault("settings", None)
            if not isinstance(data.get("drawings"), dict):
                data["drawings"] = {}
            cache[key] = data
        return cache[key]

    def save(data: dict) -> None:
        p = path()
        if p:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            type(ctx.ex)._write(p, data)

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

    @app.get("/api/chartstate")
    def get_state():
        with lock:
            return doc()

    @app.put("/api/chartstate/settings")
    def put_settings(body: dict = Body(...)):
        rec = record(body)
        with lock:
            d = doc()
            out = put(d["settings"], rec, lambda r: d.__setitem__("settings", r))
            save(d)
            return out

    @app.get("/api/chartstate/drawings/{sym}")
    def get_drawings(sym: str):
        with lock:
            return doc()["drawings"].get(symbol(sym))

    @app.put("/api/chartstate/drawings/{sym}")
    def put_drawings(sym: str, body: dict = Body(...)):
        s, rec = symbol(sym), record(body)
        if not isinstance(rec["data"], list):
            raise HTTPException(400, "drawings must be a list")
        with lock:
            d = doc()
            if s not in d["drawings"] and len(d["drawings"]) >= MAX_SYMBOLS:
                raise HTTPException(413, f"drawings are saved for at most {MAX_SYMBOLS} symbols")
            out = put(d["drawings"].get(s), rec, lambda r: d["drawings"].__setitem__(s, r))
            save(d)
            return out

    @app.delete("/api/chartstate")
    def reset_state():
        with lock:
            d = doc()
            d["settings"], d["drawings"] = None, {}
            save(d)
            return d
