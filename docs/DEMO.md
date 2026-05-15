# Demo: End-to-End Investment Query

This walkthrough shows the full pipeline for the query *"Should I invest in NVDA?"*

## Architecture Flow

```
User Query → ADK Web (8001) → ADK LlmAgent
  → LLM calls all 3 agent tools in parallel:
    → financial_rag_agent → RAG Agent (8002) → MCP (SEC EDGAR) → ChromaDB
    → quant_analysis_agent → Quant Agent (8003) → MCP (prices + financials) → LangGraph
    → sentiment_intelligence_agent → Sentiment Agent (8004) → MCP (News + SEC) → CrewAI
  → LLM synthesizes all results → BUY/HOLD/SELL recommendation
```

## Step 1: Start Services

```bat
run_adk_web.bat
```

This starts in order: MCP Server (8010) → RAG (8002) → Quant (8003) → Sentiment (8004) → ADK Web UI (8001)

## Step 2: Send Query

Open http://127.0.0.1:8001 and type: *"Should I invest in NVDA?"* or just **"NVDA"**.

The ADK orchestrator discovers all 3 agents at startup and generates one ADK tool per agent. The LLM calls all tools in response to stock queries.

## Step 3: What Happens

| Step | Component | Action |
|---|---|---|
| 1 | ADK LlmAgent | Receives query, decides which tools to call |
| 2 | Agent Tool fn | Calls `_client.send_message(agent_name, task)` |
| 3 | SubAgentClient | Lazily creates A2A client via `create_client()`, sends `SendMessageRequest` |
| 4 | Sub-agent server | `GenericAgentExecutor` runs the agent's `stream()` |
| 5 | Sub-agent logic | Each agent processes with its own tools (MCP, etc.) |
| 6 | Response | Agent yields `{response_type, content, is_task_complete}` |
| 7 | Orchestrator | `_extract_text` converts response to string |
| 8 | LLM synthesis | All agent results returned to LLM → final recommendation |

## Running the Demo

```bash
# Terminal 1 — Unified MCP Server (port 8010)
python -m uvicorn mcp_servers.finsight_server:get_app --host 0.0.0.0 --port 8010

# Terminal 2 — RAG Agent (port 8002)
python -m uvicorn agent_2_llamaindex.server:app --host 0.0.0.0 --port 8002

# Terminal 3 — Quant Agent (port 8003)
python -m uvicorn agent_3_langgraph.server:app --host 0.0.0.0 --port 8003

# Terminal 4 — Sentiment Agent (port 8004)
python -m uvicorn agent_4_crewai.server:app --host 0.0.0.0 --port 8004

# Terminal 5 — ADK Web UI (port 8001)
adk web --port 8001 agents
```

## Direct A2A Testing

Test individual agents without the ADK Web UI:

```bash
# Test RAG agent
curl -X POST http://localhost:8002/a2a \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{"jsonrpc":"2.0","method":"SendMessage","id":"1","params":{"message":{"messageId":"1","role":"ROLE_USER","parts":[{"text":"Research NVDA"}]},"metadata":{"ticker":"NVDA"}}}'

# Test Quant agent
curl -X POST http://localhost:8003/a2a \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{"jsonrpc":"2.0","method":"SendMessage","id":"1","params":{"message":{"messageId":"1","role":"ROLE_USER","parts":[{"text":"Analyze NVDA"}]},"metadata":{"ticker":"NVDA","period":"5y"}}}'

# Test Sentiment agent
curl -X POST http://localhost:8004/a2a \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{"jsonrpc":"2.0","method":"SendMessage","id":"1","params":{"message":{"messageId":"1","role":"ROLE_USER","parts":[{"text":"Sentiment for NVDA"}]},"metadata":{"ticker":"NVDA"}}}'
```
