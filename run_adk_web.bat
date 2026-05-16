@echo off
cd /d "%~dp0"

echo Starting FinSight services...

:: Terminal 0 - LM Studio inference server
start "LM Studio Server" cmd /k "lms server start"
timeout /t 5 /nobreak >nul

:: Terminal 1 - Unified MCP Server
start "FinSight MCP" cmd /c ".venv\Scripts\activate.bat && python -m uvicorn mcp_servers.finsight_server:get_app --host 0.0.0.0 --port 8010 --log-level info"
timeout /t 3 /nobreak >nul

:: Terminal 2 - RAG Agent
start "FinSight RAG" cmd /c ".venv\Scripts\activate.bat && python -m uvicorn agent_2_llamaindex.server:app --host 0.0.0.0 --port 8002 --log-level info"
timeout /t 3 /nobreak >nul

:: Terminal 3 - Quant Agent
start "FinSight Quant" cmd /c ".venv\Scripts\activate.bat && python -m uvicorn agent_3_langgraph.server:app --host 0.0.0.0 --port 8003 --log-level info"
timeout /t 3 /nobreak >nul

:: Terminal 4 - Sentiment Agent
start "FinSight Sentiment" cmd /c ".venv\Scripts\activate.bat && python -m uvicorn agent_4_crewai.server:app --host 0.0.0.0 --port 8004 --log-level info"
timeout /t 3 /nobreak >nul

:: Terminal 5 - ADK Web UI
start "ADK Web" cmd /c ".venv\Scripts\activate.bat && adk web --port 8001 agents"

echo All services starting. Allow 30-40s for boot.
echo LM Studio server: http://127.0.0.1:1234
echo ADK Web UI: http://127.0.0.1:8001
