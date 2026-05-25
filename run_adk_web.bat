@echo off
cd /d "%~dp0"

echo Starting FinSight services...

:: Terminal 0 - LM Studio inference server
start "LM Studio Server" cmd /k "lms server start"
timeout /t 5 /nobreak >nul

:: Terminal 1 - Unified MCP Server (:8010)
start "FinSight MCP" cmd /k "uv run python -m uvicorn mcp_servers.finsight_server:get_app --host 0.0.0.0 --port 8010 --log-level info"
timeout /t 3 /nobreak >nul

:: Terminal 2 - RAG Agent (:8002)
start "FinSight RAG" cmd /k "uv run python -m uvicorn agent_2_llamaindex.server:app --host 0.0.0.0 --port 8002 --log-level info"
timeout /t 3 /nobreak >nul

:: Terminal 3 - Quant Agent (:8003)
start "FinSight Quant" cmd /k "uv run python -m uvicorn agent_3_langgraph.server:app --host 0.0.0.0 --port 8003 --log-level info"
timeout /t 3 /nobreak >nul

:: Terminal 4 - Sentiment Agent (:8004)
start "FinSight Sentiment" cmd /k "uv run python -m uvicorn agent_4_crewai.server:app --host 0.0.0.0 --port 8004 --log-level info"
timeout /t 3 /nobreak >nul

:: Terminal 5 - ADK Web UI (:8080)
start "FinSight ADK Web" cmd /k "uv run adk web --port 8080 --session_service_uri sqlite://./finsight_memory.db --memory_service_uri finsight:// agents"

echo.
echo All services starting. Allow 30-40s for boot.
echo.
echo   LM Studio:    http://127.0.0.1:1234
echo   MCP Server:   http://127.0.0.1:8010
echo   RAG Agent:    http://127.0.0.1:8002
echo   Quant Agent:  http://127.0.0.1:8003
echo   Sentiment:    http://127.0.0.1:8004
echo   ADK Web UI:   http://127.0.0.1:8080
