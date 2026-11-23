@echo off
rem Start PaperDesk on http://127.0.0.1:8000 (Python 3.11+ required; Node is not needed).
cd /d "%~dp0backend"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install -q fastapi "uvicorn[standard]"
)
.venv\Scripts\python -m uvicorn app.api.server:app_factory --factory --host 127.0.0.1 --port 8000
