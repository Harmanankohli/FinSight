@echo off
cd /d "%~dp0"

echo Stopping FinSight services...

echo Killing server processes by port...
for %%p in (8001 8002 8003 8004 8010) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%p "') do (
        taskkill /f /pid %%a >nul 2>&1
    )
)

echo Closing server terminal windows...
>nul 2>&1 powershell -Command "Get-Process cmd | Where-Object { $_.MainWindowTitle -like 'FinSight*' } | Stop-Process -Force"
>nul 2>&1 powershell -Command "Get-Process cmd | Where-Object { $_.MainWindowTitle -like 'ADK*' } | Stop-Process -Force"
:: >nul 2>&1 powershell -Command "Get-Process cmd | Where-Object { $_.MainWindowTitle -like 'LM Studio*' } | Stop-Process -Force"

echo All services stopped.
