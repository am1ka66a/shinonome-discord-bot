@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python 3.11 virtual environment is missing. Please set up .venv first.
  pause
  exit /b 2
)
".venv\Scripts\python.exe" "scripts\run_local.py" %*
if errorlevel 1 pause
