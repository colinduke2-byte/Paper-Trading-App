#!/usr/bin/env bash
# Start PaperDesk on http://127.0.0.1:8000 (Python 3.11+ required; Node is not needed).
# PORT can be overridden. To use PaperDesk from a phone on the same Wi-Fi, set PAPERDESK_LAN=1 and
# PAPERDESK_PASSCODE (in .env or the environment; see README.md). Without LAN mode HOST must stay local.
set -euo pipefail
cd "$(dirname "$0")/backend"

PY=""
for cand in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "PaperDesk needs Python 3.11 or newer, and none was found on this machine." >&2
  if command -v python3 >/dev/null 2>&1; then echo "Found: $(python3 --version 2>&1)" >&2; fi
  echo "Install a newer Python from https://www.python.org/downloads/ (or your package manager) and run this again." >&2
  exit 1
fi

# (Re)install only when the virtualenv is new or pyproject.toml changed since the last install.
if [ ! -x .venv/bin/python ] || [ ! -f .venv/.installed ] || [ pyproject.toml -nt .venv/.installed ]; then
  echo "Setting up PaperDesk (first start takes a minute)..."
  [ -x .venv/bin/python ] || "$PY" -m venv .venv
  .venv/bin/python -m pip install -q -e . && touch .venv/.installed || {
    echo "Installing the Python packages failed. Check your internet connection and run this again." >&2; exit 1; }
fi

# Check the access settings (also read from ../.env) and choose the bind address: 127.0.0.1, or 0.0.0.0 in
# LAN mode. The check prints the phone addresses and reminders itself and refuses unsafe settings.
export PAPERDESK_ENV_FILE="${PAPERDESK_ENV_FILE:-$(cd .. && pwd)/.env}"
HOST="$(.venv/bin/python -m app.api.auth preflight)" || { echo "PaperDesk did not start." >&2; exit 1; }
OPEN="$HOST"; case "$HOST" in 0.0.0.0|::) OPEN=127.0.0.1 ;; esac
echo "PaperDesk is starting on http://${OPEN}:${PORT:-8000}  (press Ctrl+C to stop)"
exec .venv/bin/python -m uvicorn app.api.server:app_factory --factory --host "$HOST" --port "${PORT:-8000}" --no-proxy-headers
