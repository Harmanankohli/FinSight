# Demo: End-to-End Investment Query

This walkthrough shows the full pipeline for the query *"Should I invest in NVDA?"*

## Architecture Flow

```
User Query → ADK Web (8001) → ADK LlmAgent
  → Memory context injection (latest NVDA brief + portfolio)
  → LLM calls agents via single send_message tool (parallel with qwen):
    → send_message("Financial RAG Agent", ...) → RAG Agent (8002) → MCP (SEC EDGAR) → ChromaDB
    → send_message("Quant Analysis Agent", ...) → Quant Agent (8003) → MCP (prices + financials) → LangGraph
    → send_message("Market Context Agent", ...) → Market Context Agent (8004) → MCP (News + SEC) → CrewAI
  → LLM synthesizes all results → BUY/HOLD/SELL recommendation
  → Auto-save: brief, portfolio, performance record persisted to SQLite
```

## Memory Layer

After each query, the system automatically persists:
- **Ticker brief**: Latest recommendation for the queried ticker (BUY/HOLD/SELL + confidence + rationale)
- **Portfolio holdings**: Extracted from user query context, merged over time
- **Performance record**: Recommendation timestamped for future accuracy tracking
- **Session events**: Full conversation stored for cross-session search via `load_memory` tool

On subsequent queries, memory context is injected before the LLM runs, enabling the orchestrator to answer questions like *"Has the outlook for NVDA changed since last time?"*

## Step 1: Start Services

```bat
run_adk_web.bat
```

This starts in order: MCP Server (8010) → RAG (8002) → Quant (8003) → Sentiment (8004) → ADK Web UI (8001)

## Step 2: Send Query

Open http://127.0.0.1:8001 and type: *"Should I invest in NVDA?"* or just **"NVDA"**.

The ADK orchestrator discovers all 3 agents at startup. The LLM uses a single `send_message` tool, routing to each agent by name. With qwen, agents are called in parallel; with other models, sequentially. The LLM collects all responses and synthesizes a final recommendation.

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

# Terminal 4 — Market Context Agent (port 8004)
python -m uvicorn agent_4_crewai.server:app --host 0.0.0.0 --port 8004

# Terminal 5 — ADK Web UI (port 8001)
adk web --port 8001 agents
```

## Health Check Testing

Verify all services are up before running the demo:

```bash
curl http://localhost:8001/health  # {"status":"ok","agent":"orchestrator"}
curl http://localhost:8002/health  # {"status":"ok","agent":"rag"}
curl http://localhost:8003/health  # {"status":"ok","agent":"quant"}
curl http://localhost:8004/health  # {"status":"ok","agent":"market_context"}
curl http://localhost:8010/health  # {"status":"ok","agent":"mcp"}
```

## Guardrails in Action

Off-topic queries are rejected immediately without invoking any sub-agent:

```bash
# Off-topic → instant rejection
curl http://localhost:8001/a2a -H "Content-Type: application/json" -d '...' \
  '{"question":"what is the weather in New York?"}'
# → "I'm specialized in investment research. Please ask about stocks..."

# Invalid ticker → pre-flight rejection in < 2s
# "XXXX" fails MCP validate_ticker before RAG/Quant/Sentiment are called
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
