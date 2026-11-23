#!/usr/bin/env bash
# Start PaperDesk on http://127.0.0.1:8000 (Python 3.11+ required; Node is not needed).
set -euo pipefail
cd "$(dirname "$0")/backend"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q fastapi "uvicorn[standard]"
fi
exec .venv/bin/python -m uvicorn app.api.server:app_factory --factory --host 127.0.0.1 --port "${PORT:-8000}"
