@echo off
cd /d "%~dp0"

echo Starting FinSight services...

:: Check if REDIS_URL is set — if so, start Redis and wait for it to be ready
for /f "delims=" %%R in ('uv run python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(os.environ.get(\"REDIS_URL\",\"\"))" 2^>nul') do set REDIS_URL=%%R

if defined REDIS_URL (
    where redis-server >nul 2>&1
    if %errorlevel%==0 (
        echo [Cache] Redis ENABLED at %REDIS_URL% — starting redis-server...
        start "Redis Server" cmd /k "redis-server"
        timeout /t 2 /nobreak >nul
    ) else (
        echo [Cache] WARNING: REDIS_URL is set but redis-server not found in PATH.
        echo [Cache]          Install Redis: winget install Redis.Redis
        echo [Cache]          Falling back to in-process TTLCache.
    )
) else (
    echo [Cache] Redis DISABLED — using in-process TTLCache ^(set REDIS_URL in .env to enable^)
)

:: Terminal 0 - LM Studio inference server
start "LM Studio Server" cmd /k "lms server start"
timeout /t 5 /nobreak >nul

:: Terminal 1 - Unified MCP Server (:8010)
start "FinSight MCP" cmd /k "uv run python -m uvicorn mcp_tools.finsight_server:get_app --host 0.0.0.0 --port 8010 --log-level info"
timeout /t 3 /nobreak >nul

:: Terminal 2 - RAG Agent (:8002)
start "FinSight RAG" cmd /k "uv run python -m uvicorn financial_rag.server:app --host 0.0.0.0 --port 8002 --log-level info"
timeout /t 3 /nobreak >nul

:: Terminal 3 - Quant Agent (:8003)
start "FinSight Quant" cmd /k "uv run python -m uvicorn quant.server:app --host 0.0.0.0 --port 8003 --log-level info"
timeout /t 3 /nobreak >nul

:: Terminal 4 - Market Context Agent (:8004)
start "FinSight Market Context" cmd /k "uv run python -m uvicorn market_context.server:app --host 0.0.0.0 --port 8004 --log-level info"
timeout /t 3 /nobreak >nul

:: Terminal 5 - ADK Web UI (:8080)
start "FinSight ADK Web" cmd /k "uv run adk web --port 8080 --session_service_uri sqlite://./db/adk_sessions.db --memory_service_uri finsight:// src/orchestrator/web"

echo.
echo All services starting. Allow 30-40s for boot.
echo.
echo   LM Studio:    http://127.0.0.1:1234
echo   MCP Server:   http://127.0.0.1:8010
echo   RAG Agent:    http://127.0.0.1:8002
echo   Quant Agent:  http://127.0.0.1:8003
echo   Market Ctx:   http://127.0.0.1:8004
echo   ADK Web UI:   http://127.0.0.1:8080
