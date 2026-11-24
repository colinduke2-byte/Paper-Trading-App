"""Request hardening shared by the API: body size limits, bounded error messages, WebSocket message
checks and a per-client rate limiter. The limits themselves live in app.core.limits.

Body limits. Every /api request body is limited to limits.MAX_BODY_BYTES, checked against
Content-Length before the route runs and by counting the bytes actually received (a chunked
upload has no Content-Length). A route that needs more declares it once, when it registers:

    from .hardening import allow_body
    allow_body(app, "/api/backup/restore", 50 * limits.MB)          # exact path
    allow_body(app, "/api/things/{tid}/upload", 5 * limits.MB)       # a route template works too

A request over its limit is answered 413 with a plain message; the route never sees it.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from ..core import limits

_BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def clip(text, n: int = limits.MAX_ERROR_TEXT) -> str:
    """Shorten text for an error message or a log line, so a huge input is never echoed back."""
    s = str(text)
    return s if len(s) <= n else s[: n - 1] + "…"


def echo(value, n: int = limits.MAX_ECHO_TEXT) -> str:
    """A user-supplied value quoted inside a message."""
    return clip(value, n)


# ---- body size ----------------------------------------------------------------------------------------

def _kb(n: int) -> str:
    return f"{n // limits.MB} MB" if n >= limits.MB and n % limits.MB == 0 else f"{max(1, n // limits.KB)} KB"


def too_large_message(limit: int) -> str:
    return f"The request is too large: this server accepts at most {_kb(limit)} here."


from starlette.exceptions import HTTPException as _HTTPException  # noqa: E402


class BodyTooLarge(_HTTPException):
    """Raised while a route reads its body; FastAPI passes HTTPExceptions through, so it becomes a 413."""

    def __init__(self, limit: int):
        super().__init__(413, too_large_message(limit))
        self.limit = limit


def allow_body(app, path: str, max_bytes: int) -> None:
    """Let one route (an exact path or a route template such as /api/x/{id}) accept a larger body."""
    from starlette.routing import compile_path
    regex, _, _ = compile_path(path)
    rules = getattr(app.state, "body_limits", None)
    if rules is None:
        rules = app.state.body_limits = []
    rules.append((regex, int(max_bytes)))


def body_limit_for(app, path: str) -> int:
    for regex, n in getattr(app.state, "body_limits", None) or []:
        if regex.match(path):
            return n
    return limits.MAX_BODY_BYTES


class BodyLimitMiddleware:
    """Pure ASGI: refuse /api request bodies over their limit, by Content-Length and by counting."""

    def __init__(self, app, owner):
        self.app = app
        self.owner = owner  # the FastAPI app, which holds the per-route rules in app.state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api") \
                or scope.get("method") not in _BODY_METHODS:
            return await self.app(scope, receive, send)
        limit = body_limit_for(self.owner, scope["path"])
        for k, v in scope.get("headers") or []:
            if k.lower() == b"content-length":
                try:
                    declared = int(v)
                except ValueError:
                    return await self._refuse(scope, receive, send, 400, "The Content-Length header is not a number.")
                if declared > limit:
                    return await self._refuse(scope, receive, send, 413, too_large_message(limit))
        seen = {"n": 0, "started": False}

        async def counted():
            message = await receive()
            if message["type"] == "http.request":
                seen["n"] += len(message.get("body", b""))
                if seen["n"] > limit:
                    raise BodyTooLarge(limit)
            return message

        async def tracked(message):
            if message["type"] == "http.response.start":
                seen["started"] = True
            await send(message)

        try:
            return await self.app(scope, counted, tracked)
        except BodyTooLarge as e:
            if seen["started"]:
                raise
            return await self._refuse(scope, receive, send, 413, too_large_message(e.limit))

    @staticmethod
    async def _refuse(scope, receive, send, status: int, detail: str):
        from starlette.responses import JSONResponse
        await JSONResponse({"detail": detail}, status_code=status, headers={"connection": "close"})(scope, receive, send)


def validation_errors(errors) -> list:
    """FastAPI's 422 answer without the echoed input, and with at most a few entries."""
    out = []
    for e in list(errors)[: limits.MAX_VALIDATION_ERRORS]:
        loc = [clip(x, limits.MAX_ECHO_TEXT) if isinstance(x, str) else x for x in (e.get("loc") or [])][:6]
        out.append({"loc": loc, "msg": clip(e.get("msg", ""), 200), "type": clip(e.get("type", ""), 60)})
    return out


# ---- WebSocket messages -----------------------------------------------------------------------------------

class BadMessage(ValueError):
    def __init__(self, message: str, code: int = limits.WS_CLOSE_POLICY):
        super().__init__(message)
        self.code = code


def ws_update(text: Optional[str], sub: dict) -> None:
    """Apply one message from a browser to its subscription, or raise BadMessage.

    A message is a JSON object with optional keys "symbols" (a list of short strings; only the first
    MAX_WS_SYMBOLS are kept) and "account" (an account id string, or null).
    """
    import json
    if text is None:
        raise BadMessage("send text messages only", 1003)
    if len(text) > limits.MAX_WS_MESSAGE:
        raise BadMessage("message too large", limits.WS_CLOSE_TOO_BIG)
    try:
        msg = json.loads(text)
    except ValueError:
        raise BadMessage("message is not JSON") from None
    if not isinstance(msg, dict):
        raise BadMessage("message must be a JSON object")
    if "symbols" in msg:
        syms = msg["symbols"]
        if not isinstance(syms, list):
            raise BadMessage("symbols must be a list")
        syms = syms[: limits.MAX_WS_SYMBOLS]
        if not all(isinstance(s, str) and 0 < len(s) <= limits.MAX_SYMBOL_TEXT for s in syms):
            raise BadMessage("symbols must be short strings")
        sub["symbols"] = list(dict.fromkeys(s.upper() for s in syms))
    if "account" in msg:
        acct = msg["account"]
        if acct is not None and not (isinstance(acct, str) and len(acct) <= limits.MAX_SYMBOL_TEXT):
            raise BadMessage("account must be an account id or null")
        sub["account"] = acct


# ---- per-client rate limit ----------------------------------------------------------------------------------

class ClientRate:
    """Token bucket per client address, for GETs that spend a provider's request budget."""

    MAX_CLIENTS = 256

    def __init__(self, per_minute: float = limits.RESEARCH_RATE_PER_MINUTE, burst: float = limits.RESEARCH_RATE_BURST,
                 clock: Callable[[], float] = time.monotonic):
        self.rate, self.burst, self.clock = per_minute / 60.0, float(burst), clock
        self.buckets: dict = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> float:
        """0 when allowed (and a token is taken); otherwise seconds until the next request is allowed."""
        now = self.clock()
        with self.lock:
            tokens, last = self.buckets.pop(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            wait = 0.0 if tokens >= 1 else (1 - tokens) / self.rate
            if not wait:
                tokens -= 1
            self.buckets[key] = (tokens, now)  # re-inserted: the dict is in least-recently-used order
            while len(self.buckets) > self.MAX_CLIENTS:
                self.buckets.pop(next(iter(self.buckets)))
            return wait


# ---- file permissions -------------------------------------------------------------------------------------

def private_dir(path: str) -> None:
    """Create the data directory readable by this user only (POSIX 0700). An existing one is left alone."""
    import os
    if not os.path.isdir(path):
        os.makedirs(path, mode=0o700, exist_ok=True)


def permission_warnings(paths: list) -> list:
    """Plain warnings for files or directories other users of this computer can read (POSIX only).

    The data directory holds account state and sign-in digests; .env can hold the passcode and provider
    keys. On Windows, files under your user profile are private to your account by default, so nothing
    is checked there.
    """
    import os
    import stat
    if os.name != "posix":
        return []
    out = []
    for p in paths:
        try:
            mode = os.stat(p).st_mode
        except OSError:
            continue
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            fix = "chmod 700" if stat.S_ISDIR(mode) else "chmod 600"
            out.append(f"{p} can be read by other users of this computer (mode {stat.S_IMODE(mode):o}); run: {fix} {p}")
    return out
