"""Request limits for every route: the /api body size limit and a bounded 422 validation answer.

The limits are constants in app.core.limits (docs/LIMITS.md); hardening.py holds the mechanisms.
A route that needs a larger body calls hardening.allow_body(app, path, max_bytes) when it registers.
"""
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import hardening


def register(app, ctx):
    app.add_middleware(hardening.BodyLimitMiddleware, owner=app)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_, exc):
        # FastAPI's default answer echoes the whole input back; this one names the fields only.
        return JSONResponse({"detail": hardening.validation_errors(exc.errors())}, status_code=422)
