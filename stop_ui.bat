@echo off
cd /d "%~dp0"

echo Stopping FinSight UI services...

:: Kill by port — covers all backend + frontend ports
for %%p in (3000 8001 8002 8003 8004 8010) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%p " 2^>nul') do (
        taskkill /f /pid %%a >nul 2>&1
    )
)

:: Stop Redis if configured
for /f "delims=" %%R in ('uv run python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(os.environ.get(\"REDIS_URL\",\"\"))" 2^>nul') do set REDIS_URL=%%R
if defined REDIS_URL (
    where redis-server >nul 2>&1
    if %errorlevel%==0 (
        echo Stopping Redis...
        taskkill /f /im redis-server.exe >nul 2>&1
        powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*redis-server*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
)

:: Close terminal windows started by this script
powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -ne $null -and ($_.CommandLine -like '*uv run*' -or $_.CommandLine -like '*lms server*' -or $_.CommandLine -like '*npm run dev*' -or $_.CommandLine -like '*redis-server*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo Done. All UI-mode services stopped.
