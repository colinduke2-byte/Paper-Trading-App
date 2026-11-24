"""Symbol lookups for the UI.

GET /api/symbols/lookup?symbols=A,B,C -> [{"symbol", "name", "sector", "type"}] for the ones the active feed
knows, in the order asked. The UI uses it to check a symbol remembered in the browser (for example a
fictional symbol from the simulated feed) before showing it, without downloading the whole symbol list.
"""
from fastapi import Query


def register(app, ctx):
    @app.get("/api/symbols/lookup")
    def lookup(symbols: str = Query("")):
        info = ctx.ex.feed.symbols()
        out = []
        for s in dict.fromkeys(x.strip().upper() for x in symbols.split(",") if x.strip()):
            i = info.get(s)
            if i is not None and len(out) < 200:
                out.append({"symbol": s, "name": i["name"], "sector": i.get("sector", ""), "type": i.get("type", "stock")})
        return out
