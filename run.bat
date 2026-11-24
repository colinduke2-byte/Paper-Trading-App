@echo off
setlocal EnableExtensions
rem Start PaperDesk on http://127.0.0.1:8000 (Python 3.11+ required; Node is not needed).
rem PORT can be set beforehand. To use PaperDesk from a phone on the same Wi-Fi, set PAPERDESK_LAN=1 and
rem PAPERDESK_PASSCODE (in .env next to this file, or the environment; see README.md).
rem Set PAPERDESK_NO_BROWSER=1 to skip opening the browser, PAPERDESK_NO_PAUSE=1 to skip the final pause.
cd /d "%~dp0backend" || goto :fail
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
  %PY% -c "import os,sys; t=os.path.getmtime('.venv/.installed'); sys.exit(0 if t >= os.path.getmtime('pyproject.toml') and t >= os.path.getmtime('constraints.txt') else 1)" >nul 2>&1
  if errorlevel 1 set "NEED=1"
)
if "%NEED%"=="1" (
  echo Setting up PaperDesk ^(first start takes a minute^)...
  if not exist ".venv\Scripts\python.exe" ( %PY% -m venv .venv || goto :fail )
  rem constraints.txt pins the exact versions the tests passed with (tools\backend_constraints.py refreshes it).
  ".venv\Scripts\python.exe" -m pip install -q -c constraints.txt -e . || goto :fail
  echo done> ".venv\.installed"
)

rem Check the access settings (also read from .env) and choose the bind address: 127.0.0.1, or 0.0.0.0 in
rem LAN mode. The check prints the phone addresses and reminders itself (on stderr) and prints only the
rem bind address on stdout; it prints nothing there when the settings are unsafe.
if not defined PAPERDESK_ENV_FILE set "PAPERDESK_ENV_FILE=%~dp0.env"
set "BINDHOST="
for /f "usebackq delims=" %%h in (`.venv\Scripts\python.exe -m app.api.auth preflight`) do set "BINDHOST=%%h"
if not defined BINDHOST goto :fail
set "HOST=%BINDHOST%"
set "OPENHOST=%HOST%"
if "%HOST%"=="0.0.0.0" set "OPENHOST=127.0.0.1"
if "%HOST%"=="::" set "OPENHOST=127.0.0.1"

if not defined PAPERDESK_NO_BROWSER start "" /b ".venv\Scripts\python.exe" "%~dp0tools\open_when_ready.py" "http://%OPENHOST%:%PORT%"
echo PaperDesk is starting on http://%OPENHOST%:%PORT%  ^(close this window or press Ctrl+C to stop^)
".venv\Scripts\python.exe" -m uvicorn app.api.server:app_factory --factory --host "%HOST%" --port "%PORT%" --no-proxy-headers
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo PaperDesk could not start. The message above says why.
if not defined PAPERDESK_NO_PAUSE pause
exit /b 1
