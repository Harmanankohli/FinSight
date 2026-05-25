@echo off
cd /d "%~dp0"

echo Stopping FinSight services...

echo Killing server processes by port...
for %%p in (8001 8002 8003 8004 8010 8080) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%p "') do (
        taskkill /f /pid %%a >nul 2>&1
    )
)

echo Closing terminal windows...
powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -ne $null -and ($_.CommandLine -like '*uv run*' -or $_.CommandLine -like '*lms server*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo All services stopped.
