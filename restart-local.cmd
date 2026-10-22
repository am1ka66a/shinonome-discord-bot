@echo off
setlocal
cd /d "%~dp0"
call stop-local.cmd
if errorlevel 1 exit /b 1
call run-local.cmd
