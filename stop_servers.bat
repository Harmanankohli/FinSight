@echo off
cd /d "%~dp0"

echo Stopping FinSight services...

for %%p in (8001 8002 8003 8004 8010 1234) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%p "') do (
        taskkill /f /pid %%a >nul 2>&1
    )
)

echo Stopping service windows...
taskkill /f /fi "WINDOWTITLE eq FinSight*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq ADK Web*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq LM Studio*" >nul 2>&1

echo All services stopped.
