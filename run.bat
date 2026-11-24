@echo off
setlocal EnableExtensions
rem Start PaperDesk on http://127.0.0.1:8000 (Python 3.11+ required; Node is not needed).
rem HOST and PORT can be set beforehand. HOST stays 127.0.0.1: PaperDesk has no login, never expose it.
rem Set PAPERDESK_NO_BROWSER=1 to skip opening the browser, PAPERDESK_NO_PAUSE=1 to skip the final pause.
cd /d "%~dp0backend" || goto :fail
if not defined HOST set "HOST=127.0.0.1"
if not defined PORT set "PORT=8000"

set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo PaperDesk needs Python 3.11 or newer, and none was found.
  echo Install it from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH", then run this again.
  goto :fail
)

set "NEED=0"
if not exist ".venv\Scripts\python.exe" set "NEED=1"
if not exist ".venv\.installed" set "NEED=1"
if "%NEED%"=="0" (
  %PY% -c "import os,sys; sys.exit(0 if os.path.getmtime('.venv/.installed') >= os.path.getmtime('pyproject.toml') else 1)" >nul 2>&1
  if errorlevel 1 set "NEED=1"
)
if "%NEED%"=="1" (
  echo Setting up PaperDesk ^(first start takes a minute^)...
  if not exist ".venv\Scripts\python.exe" ( %PY% -m venv .venv || goto :fail )
  ".venv\Scripts\python.exe" -m pip install -q -e . || goto :fail
  echo done> ".venv\.installed"
)

if not defined PAPERDESK_NO_BROWSER start "" /b ".venv\Scripts\python.exe" "%~dp0tools\open_when_ready.py" "http://%HOST%:%PORT%"
echo PaperDesk is starting on http://%HOST%:%PORT%  ^(close this window or press Ctrl+C to stop^)
".venv\Scripts\python.exe" -m uvicorn app.api.server:app_factory --factory --host %HOST% --port %PORT%
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo PaperDesk could not start. The message above says why.
if not defined PAPERDESK_NO_PAUSE pause
exit /b 1
