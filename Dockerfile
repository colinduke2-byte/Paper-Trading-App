# syntax=docker/dockerfile:1

# ---- stage 1: build the web UI -------------------------------------------------------------
# Base images are pinned by digest (multi-arch index digests read from Docker Hub on 2026-11-24). To update,
# read the new digest with `docker buildx imagetools inspect node:22-alpine` (and python:3.13-slim) and rebuild.
FROM node:22-alpine@sha256:1314fb30ec37592d5845e5a160d522c0e680972aa534549940b8d73bde88481a AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- stage 2: the server -------------------------------------------------------------------
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PAPERDESK_DATA=/data \
    PAPERDESK_UI=/app/frontend/dist
WORKDIR /app/backend

RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin paperdesk \
 && mkdir /data && chown paperdesk /data

COPY backend/pyproject.toml backend/constraints.txt ./
COPY backend/app ./app
RUN pip install -c constraints.txt -e .
COPY --from=ui /ui/dist /app/frontend/dist
COPY THIRD_PARTY_NOTICES.md /app/

USER paperdesk
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/api/auth/state' % os.environ.get('PORT','8000'), timeout=4)"]

# 0.0.0.0 is for inside the container only. The compose file publishes the port on 127.0.0.1 of the host
# unless LAN mode is turned on (PAPERDESK_LAN=1, PAPERDESK_PASSCODE and PAPERDESK_BIND=0.0.0.0; see README.md).
CMD ["sh", "-c", "exec python -m uvicorn app.api.server:app_factory --factory --host \"${HOST:-0.0.0.0}\" --port \"${PORT:-8000}\" --no-proxy-headers"]
