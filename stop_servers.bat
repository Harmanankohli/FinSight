@echo off
cd /d "%~dp0"

echo Stopping FinSight services...

echo Killing server processes by port...
for %%p in (8001 8002 8003 8004 8005 8006 8010 8080) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%p "') do (
        taskkill /f /pid %%a >nul 2>&1
    )
)

:: Stop Redis if it was started (only if REDIS_URL is set in .env)
for /f "delims=" %%R in ('uv run python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(os.environ.get(\"REDIS_URL\",\"\"))" 2^>nul') do set REDIS_URL=%%R
if defined REDIS_URL (
    where redis-server >nul 2>&1
    if %errorlevel%==0 (
        echo Stopping Redis server...
        taskkill /f /im redis-server.exe >nul 2>&1
        powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*redis-server*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
)

echo Closing terminal windows...
powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -ne $null -and ($_.CommandLine -like '*uv run*' -or $_.CommandLine -like '*lms server*' -or $_.CommandLine -like '*redis-server*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo All services stopped.
