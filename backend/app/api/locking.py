"""Puts every API route under the exchange lock by default (see docs/CONCURRENCY.md).

`install(app, ctx)` makes this the route class of the app, so a route added anywhere (server.py or a
routes_*.py module) is covered without doing anything:

- a plain `def` route under /api runs entirely while holding ctx.lock: it sees one exchange, in one
  consistent state, and what it changes is applied as a unit. If the lock cannot be taken within
  ROUTE_WAIT seconds the answer is 503 "busy" with Retry-After;
- its result is copied before the lock is released, so the response never aliases live state;
- a route that may block (provider network calls, reading session files) is marked `@unlocked("why")`
  and takes the lock itself, briefly, with `with ctx.locked():` around the state it reads;
- an `async def` route under /api must be marked `@unlocked`: it runs on the event loop, which must
  never wait for a thread lock. Registration fails otherwise.

The sign-in routes, the research routes (provider data only) and the chart-state routes (their own file
and lock) are exempt by path.
"""
import asyncio
import functools
import os

from fastapi.responses import Response
from fastapi.routing import APIRoute

from ..core.locking import Busy

ROUTE_WAIT = float(os.environ.get("PAPERDESK_LOCK_WAIT", "2.0"))  # seconds a request waits before "busy"
# These never touch exchange state: sign-in (its own session store), research (provider data only) and
# saved chart settings (their own file and lock; they write to disk, which must not happen under the lock).
EXEMPT_PREFIXES = ("/api/auth/", "/api/research/", "/api/chartstate")
_MARK = "__paperdesk_lock__"


def unlocked(reason: str):
    """Mark a route that must not run under the exchange lock, and say why."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("say why the route runs without the lock")

    def mark(fn):
        setattr(fn, _MARK, ("unlocked", reason))
        return fn
    return mark


def detach(value):
    """Copy the containers of a route result, so nothing in it changes after the lock is released."""
    if isinstance(value, dict):
        return {k: detach(v) for k, v in value.items()}
    if isinstance(value, list):
        return [detach(v) for v in value]
    if isinstance(value, tuple):
        return tuple(detach(v) for v in value)
    return value


def wait_seconds() -> float:
    return ROUTE_WAIT


def install(app, ctx) -> None:
    """Call before any route is registered."""

    class LockedRoute(APIRoute):
        def __init__(self, path, endpoint, **kw):
            mark = getattr(endpoint, _MARK, None)
            if mark is not None:
                self.lock_policy, self.lock_reason = mark
            elif not path.startswith("/api/"):
                self.lock_policy, self.lock_reason = "unlocked", "not an API route"
            elif path.startswith(EXEMPT_PREFIXES):
                self.lock_policy, self.lock_reason = "unlocked", "exempt path: does not touch exchange state"
            elif asyncio.iscoroutinefunction(endpoint):
                raise TypeError(f"{path}: an async API route cannot wait for the exchange lock. Make it a plain "
                                f"def, or mark it @unlocked and take ctx.locked() in a worker thread.")
            else:
                self.lock_policy, self.lock_reason = "locked", ""
                endpoint = _guard(ctx, endpoint, path)
            super().__init__(path, endpoint, **kw)

    app.router.route_class = LockedRoute


def _guard(ctx, endpoint, path: str):
    @functools.wraps(endpoint)
    def guarded(*args, **kwargs):
        before = getattr(ctx, "before_lock", None)
        if before is not None:  # provider work the route will need, done before the lock is taken
            before(kwargs)
        with ctx.lock.hold(wait_seconds(), label=path):
            result = endpoint(*args, **kwargs)
            return result if isinstance(result, Response) else detach(result)
    return guarded


def busy_response(_request, exc: Busy):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": str(exc), "busy": True}, status_code=503, headers={"Retry-After": "1"})
