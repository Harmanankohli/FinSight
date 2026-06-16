@echo off
cd /d "%~dp0"
set "FINRIGHT_SRC=%~dp0src"

echo Starting FinSight services (AG-UI mode)...
echo.

:: ── Redis (optional) ────────────────────────────────────────────────────────
for /f "delims=" %%R in ('uv run python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(os.environ.get(\"REDIS_URL\",\"\"))" 2^>nul') do set REDIS_URL=%%R

if defined REDIS_URL (
    where redis-server >nul 2>&1
    if %errorlevel%==0 (
        echo [Cache] Redis ENABLED at %REDIS_URL% — starting redis-server...
        start "Redis Server" cmd /k "redis-server"
        timeout /t 2 /nobreak >nul
    ) else (
        echo [Cache] WARNING: REDIS_URL set but redis-server not found. Falling back to TTLCache.
    )
) else (
    echo [Cache] Redis DISABLED — using in-process TTLCache
)

:: ── LM Studio (:1234) ────────────────────────────────────────────────────────
start "LM Studio Server" cmd /k "lms server start"
timeout /t 5 /nobreak >nul

:: ── MCP Server (:8010) ───────────────────────────────────────────────────────
start "FinSight MCP" cmd /k "set "PYTHONPATH=%FINRIGHT_SRC%" && uv run python -m uvicorn mcp_tools.finsight_server:get_app --host 0.0.0.0 --port 8010 --log-level info"
timeout /t 3 /nobreak >nul

:: ── RAG Agent (:8002) ────────────────────────────────────────────────────────
start "FinSight RAG" cmd /k "set "PYTHONPATH=%FINRIGHT_SRC%" && uv run python -m uvicorn financial_rag.server:app --host 0.0.0.0 --port 8002 --log-level info"
timeout /t 3 /nobreak >nul

:: ── Quant Agent (:8003) ──────────────────────────────────────────────────────
start "FinSight Quant" cmd /k "set "PYTHONPATH=%FINRIGHT_SRC%" && uv run python -m uvicorn quant.server:app --host 0.0.0.0 --port 8003 --log-level info"
timeout /t 3 /nobreak >nul

:: ── Market Context Agent (:8004) ─────────────────────────────────────────────
start "FinSight Market Context" cmd /k "set "PYTHONPATH=%FINRIGHT_SRC%" && uv run python -m uvicorn market_context.server:app --host 0.0.0.0 --port 8004 --log-level info"
timeout /t 3 /nobreak >nul

:: ── Analytics Agent (:8005) ──────────────────────────────────────────────────
start "FinSight Analytics" cmd /k "set "PYTHONPATH=%FINRIGHT_SRC%" && uv run python -m uvicorn analytics.server:app --host 0.0.0.0 --port 8005 --log-level info"
timeout /t 3 /nobreak >nul

:: ── Reviewer Agent (:8006) ───────────────────────────────────────────────────
start "FinSight Reviewer" cmd /k "set "PYTHONPATH=%FINRIGHT_SRC%" && uv run python -m uvicorn reviewer.server:app --host 0.0.0.0 --port 8006 --log-level info"
timeout /t 3 /nobreak >nul

:: ── Orchestrator A2A + AG-UI bridge (:8001) ──────────────────────────────────
:: Runs src/orchestrator/main.py — exposes /a2a, /a2a-agui, /api/*, /health
start "FinSight Orchestrator" cmd /k "set "PYTHONPATH=%FINRIGHT_SRC%" && uv run python -m orchestrator.main"
timeout /t 5 /nobreak >nul

:: ── Next.js UI (:3000) ───────────────────────────────────────────────────────
start "FinSight UI" cmd /k "cd src\web\nextjs-app && npm run dev"

echo.
echo All services starting. Allow 30-40s for boot.
echo.
echo   LM Studio:     http://127.0.0.1:1234
echo   MCP Server:    http://127.0.0.1:8010
echo   RAG Agent:     http://127.0.0.1:8002
echo   Quant Agent:   http://127.0.0.1:8003
echo   Market Ctx:    http://127.0.0.1:8004
echo   Analytics:     http://127.0.0.1:8005
echo   Reviewer:      http://127.0.0.1:8006
echo   Orchestrator:  http://127.0.0.1:8001   (A2A + /a2a-agui + REST API)
echo   FinSight UI:   http://127.0.0.1:3000   ^<-- open this
echo.
echo To stop all services run: stop_ui.bat
