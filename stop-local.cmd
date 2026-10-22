@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; if (-not (Test-Path '.local-state/process.json')) { Write-Host 'No background Bot record found.'; exit 0 }; $info=Get-Content '.local-state/process.json' -Raw | ConvertFrom-Json; $p=Get-Process -Id $info.pid -ErrorAction SilentlyContinue; if (-not $p) { Write-Host 'Background Bot is already stopped.'; exit 0 }; if ([math]::Abs(($p.StartTime-[datetime]$info.startedAt).TotalSeconds) -gt 1 -or $p.Path -ne $info.executable) { throw 'Process identity changed. Stop cancelled.' }; Get-CimInstance Win32_Process -Filter ('ParentProcessId = '+$info.pid) | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*scripts/run_local.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }; Stop-Process -Id $info.pid -ErrorAction SilentlyContinue; Write-Host 'Local background Bot stopped.'"
if errorlevel 1 pause
