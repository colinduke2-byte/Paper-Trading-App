"""Sign-in endpoints and the access middleware (see auth.py for the policy).

POST /api/auth/login   {"passcode": "..."} -> {"ok": true} and a session cookie, or {"ok": false, "detail": ...}.
                       A wrong passcode answers 200 with ok false (so the browser does not log an error for an
                       expected outcome); too many attempts answer 429 with Retry-After.
POST /api/auth/logout  ends this browser's session and clears the cookie.
GET  /api/auth/state   {"auth_required", "authenticated", "bypass", "lan"}; always public.
"""
import math

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from . import auth

MAX_BODY = 4096

# Served for server-rendered pages (see auth.PUBLIC_API): any element with a data-print attribute prints.
PRINT_JS = ('document.addEventListener("click", function (e) {\n'
            '  var t = e.target && e.target.closest ? e.target.closest("[data-print]") : null;\n'
            '  if (t) { e.preventDefault(); window.print(); }\n'
            '});\n')


def register(app, ctx):
    config = auth.load_config()          # raises auth.ConfigError: the server does not start
    # Sessions live with the LIVE exchange's data directory: a replay swaps ctx.ex for a sandbox with its
    # own directory, and sign-ins must not follow it.
    live = getattr(ctx, "live", None) or ctx.ex
    access = auth.Access(config, data_dir=getattr(live, "data_dir", None))
    access.app = app
    app.state.access = access
    app.add_middleware(auth.AccessMiddleware, access=access)
    if config.lan or config.auth:
        auth.log.info("passcode required for browsers%s; LAN mode %s; sign-ins %s",
                      " except on this computer" if config.loopback_bypass else "",
                      "on" if config.lan else "off",
                      f"saved in {access.sessions.path}" if access.sessions.path else "kept in memory only")

    def authenticated(request: Request) -> bool:
        return access.sessions.check(request.cookies.get(auth.COOKIE))

    @app.get("/api/auth/state")
    def state(request: Request):
        bypass = access.config.auth and access.bypassed(request.scope)
        return {"auth_required": access.config.auth, "lan": access.config.lan, "bypass": bypass,
                "authenticated": (not access.config.auth) or bypass or authenticated(request)}

    @app.post("/api/auth/login")
    async def login(request: Request):
        if not access.config.auth:
            return JSONResponse({"detail": "This server has no passcode set, so there is nothing to sign in to."},
                                status_code=400)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY:
                return JSONResponse({"detail": "The sign-in request is too large."}, status_code=413)
        import json
        try:
            data = json.loads(body or b"null")
        except ValueError:
            data = None
        passcode = data.get("passcode") if isinstance(data, dict) else None
        if not isinstance(passcode, str):
            return JSONResponse({"detail": 'Send the passcode as JSON: {"passcode": "..."}.'}, status_code=400)

        # From here to the end there is no await, so concurrent attempts cannot slip past the limiter.
        client = request.client.host if request.client else None
        key = auth.client_key(client)
        wait = access.limiter.retry_after(key)
        if wait > 0:
            secs = max(1, math.ceil(wait))
            auth.log.warning("sign-in from %s refused: too many failed attempts (retry in %ds)", client, secs)
            return JSONResponse({"detail": f"Too many wrong passcodes. Try again in {secs} second{'' if secs == 1 else 's'}.",
                                 "retry_after": secs}, status_code=429, headers={"Retry-After": str(secs)})
        if not access.check_passcode(passcode):
            failures, delay = access.limiter.failed(key)
            auth.log.warning("failed sign-in from %s (failure %d in a row%s)", client, failures,
                             f"; next attempt allowed in {math.ceil(delay)}s" if delay else "")
            return {"ok": False, "detail": "That passcode is not correct.",
                    "retry_after": math.ceil(delay) if delay else 0}
        access.limiter.succeeded(key)
        token = access.sessions.create()
        auth.log.info("signed in from %s", client)
        resp = JSONResponse({"ok": True})
        resp.raw_headers.append((b"set-cookie", auth.cookie_header(
            token, int(access.sessions.idle), request.url.scheme == "https").encode()))
        return resp

    @app.get("/print.js", include_in_schema=False)
    def print_js():
        return Response(PRINT_JS, media_type="text/javascript", headers={"Cache-Control": "max-age=3600"})

    @app.post("/api/auth/logout")
    def logout(request: Request):
        access.sessions.revoke(request.cookies.get(auth.COOKIE))
        resp = JSONResponse({"ok": True})
        resp.raw_headers.append((b"set-cookie", auth.cookie_header("", 0, request.url.scheme == "https").encode()))
        return resp
