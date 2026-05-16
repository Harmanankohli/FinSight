# FinSight — Multi-Agent Investment Research System

An autonomous multi-agent system that answers investment queries like *"Should I invest in NVIDIA?"* by coordinating three specialized agents across different frameworks, communicating via the **Agent-to-Agent (A2A)** protocol, and using **MCP (Model Context Protocol)** servers for external tool access.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│              ADK Web UI (port 8001)                           │
│           Orchestrator (ADK LlmAgent)                        │
│         Discovers agents → LLM routes via send_message       │
│         Single tool: send_message(name, task)                │
└──────────────────────┬───────────────────────────────────────┘
                       │ A2A Protocol (JSON-RPC over HTTP, streaming)
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  Agent Pool                                                   │
│  RAG (:8002)    Quant (:8003)    Sentiment (:8004)           │
│  (LlamaIndex)   (LangGraph)      (CrewAI)                    │
└────────┬────────────┬────────────────┬───────────────────────┘
         │            │                │
         ▼            ▼                ▼
┌──────────────────────────────────────────────────────────────┐
│          Unified finsight-mcp Server (port 8010)              │
│  ┌─────────────────┐  ┌──────────────────────────────────┐   │
│  │ Agent Registry  │  │  Data Sources                    │   │
│  │ find_agent()    │  │  get_prices, get_financials,     │   │
│  │ resource://cards│  │  get_company_filings,            │   │
│  └─────────────────┘  │  full_text_search,              │   │
│                        │  get_news_sentiment,            │   │
│                        │  execute_python, ...            │   │
│                        └──────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

## Agents

| Agent | Framework | Port | Agent Card | Executor |
|---|---|---|---|---|
| **Orchestrator** | Google ADK | 8001 | Built programmatically in `main.py` | ADK `LlmAgent` with `send_message` tool |
| **RAG** | LlamaIndex + ChromaDB | 8002 | Built programmatically in `server.py` | `GenericAgentExecutor(RAGAgent)` |
| **Quant** | LangGraph + MCP | 8003 | Built programmatically in `server.py` | `GenericAgentExecutor(QuantAgent)` |
| **Sentiment** | CrewAI | 8004 | Built programmatically in `server.py` | `GenericAgentExecutor(SentimentAgent)` |
| **MCP Server** | FastMCP | 8010 | Loaded from `agent_cards/*.json` | Registry + all data tools |

### How the Orchestrator Works

The orchestrator uses a single ADK `LlmAgent` with one `send_message` tool. The LLM routes requests to sub-agents by name:

1. **Discovers agents in background** — Uses `A2ACardResolver` (standard `/.well-known/agent-card.json` endpoint) to discover sub-agents at startup. Retries up to 3 times for slow-starting agents.
2. **LLM routes to each agent** — The LLM calls `send_message(agent_name, task)` for each agent, one at a time, with a detailed task description. The agents' names and capabilities are listed in the system prompt.
3. **Synthesizes results** — After all agents respond, the LLM produces a BUY/HOLD/SELL recommendation with supporting evidence.

All A2A communication uses `A2ACardResolver` for standard discovery and `ClientFactory` for transport. Streaming events are handled correctly: intermediate `WORKING`/`SUBMITTED` events are skipped, only actual results (`artifact_update` data or terminal `COMPLETED` status) are returned.

## Tech Stack

| Layer | Technology |
|---|---|
| Agent Communication | Google A2A Protocol (JSON-RPC over HTTP, streaming) |
| Orchestrator | Google ADK `LlmAgent` with `send_message` tool |
| Sub-agent Executor | `GenericAgentExecutor` + `BaseAgent` pattern |
| RAG | LlamaIndex + ChromaDB (local) + HuggingFace embeddings |
| Quant | LangChain + LangGraph (state machine, MCP data) |
| Sentiment | CrewAI (parallel data collection + synthesis) |
| MCP Server | FastMCP (agent registry + data tools) |
| LLM | LM Studio (local, OpenAI-compatible) |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2, local) |
| Vector Store | ChromaDB (local, persisted) |
| Agent Discovery | `A2ACardResolver` via `AGENT_SEED_URLS` |

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- [LM Studio](https://lmstudio.ai) with a model loaded (e.g. `gpt-oss-20b`) on port 1234

### Setup

```bash
git clone https://github.com/Harmanankohli/FinSight.git
cd multi-agent-investment-system

# Create virtualenv & install
uv venv --python 3.12
.venv\Scripts\activate
uv pip install -e ".[dev]"
uv pip install sentence-transformers

# Copy configuration template
copy .env.example .env
# Edit .env if needed (model name, port, etc.)
```

### Run All Services

Use the batch file to start everything:

```bat
run_adk_web.bat
```

Or start each service manually in separate terminals:

```bash
# Terminal 0: LM Studio inference server
lms server start

# Terminal 1: Unified MCP Server
uv run python -m uvicorn mcp_servers.finsight_server:get_app --host 0.0.0.0 --port 8010

# Terminal 2: RAG Agent
uv run python -m uvicorn agent_2_llamaindex.server:app --host 0.0.0.0 --port 8002

# Terminal 3: Quant Agent
uv run python -m uvicorn agent_3_langgraph.server:app --host 0.0.0.0 --port 8003

# Terminal 4: Sentiment Agent
uv run python -m uvicorn agent_4_crewai.server:app --host 0.0.0.0 --port 8004

# Terminal 5: ADK Web UI
.venv\Scripts\activate && adk web --port 8001 agents
```

**Startup order:** LM Studio → MCP Server → RAG → Quant → Sentiment → ADK Web UI

Open http://127.0.0.1:8001 in your browser.

### Stop All Services

```bat
stop_servers.bat
```

## Project Structure

```
├── agent_1_adk/              # ADK Orchestrator
│   ├── agent.py              # LlmAgent with single send_message tool
│   ├── agent_executor.py     # FinSightAgentExecutor (A2A server runtime)
│   ├── sub_agent_client.py   # SubAgentClient — async A2ACardResolver + ClientFactory
│   └── main.py               # A2A server entrypoint (click + uvicorn)
│
├── agent_2_llamaindex/       # RAG Agent
│   ├── server.py             # GenericAgentExecutor(RAGAgent)
│   ├── executor.py           # RAGAgent extends BaseAgent with stream()
│   ├── index_manager.py      # ChromaDB multi-index + LM Studio LLM
│   ├── hybrid_search.py      # BM25 + dense + RRF + reranker
│   └── document_ingestion.py # MCP ingestion pipeline
│
├── agent_3_langgraph/        # Quant Agent
│   ├── server.py             # GenericAgentExecutor(QuantAgent)
│   ├── executor.py           # QuantAgent extends BaseAgent with stream()
│   ├── graph.py              # LangGraph state machine
│   ├── nodes.py              # Nodes + LM Studio LLM summary
│   └── state.py              # QuantAnalysisState schema
│
├── agent_4_crewai/           # Sentiment Agent
│   ├── server.py             # GenericAgentExecutor(SentimentAgent)
│   ├── executor.py           # SentimentAgent extends BaseAgent with stream()
│   ├── crew.py               # 2-agent CrewAI (analysis + synthesis)
│   └── mcp_tools.py          # DynamicMCPTool with Pydantic args_schema
│
├── agents/finsight_agent/    # ADK Web-compatible agent entrypoint
│   ├── __init__.py           # Re-exports root_agent from agent_1_adk.agent
│   └── agent.py              # Thin re-export wrapper
│
├── agent_cards/              # Declarative A2A Agent Card JSON files (MCP registry)
│   ├── orchestrator_agent.json
│   ├── rag_agent.json
│   ├── quant_agent.json
│   └── sentiment_agent.json
│
├── mcp_servers/              # Unified MCP Server
│   ├── finsight_server.py    # Registry + all data tools (port 8010)
│   └── Dockerfile
│
├── shared/                   # Shared libraries
│   ├── base_agent.py         # BaseAgent abstract class
│   ├── generic_executor.py   # GenericAgentExecutor
│   ├── workflow.py           # WorkflowGraph state machine
│   ├── types.py              # Shared Pydantic models
│   ├── config.py             # Centralized .env configuration
│   ├── mcp_client.py         # MCP client with dynamic tool discovery
│   └── models.py             # Pydantic data models
│
├── tests/                    # Test suite (42 tests)
│   ├── test_a2a_communication.py
│   ├── test_agent_cards.py
│   ├── test_base_agent.py
│   ├── test_orchestrator_tools.py
│   ├── test_planner.py
│   ├── test_quant_graph.py
│   ├── test_rag_pipeline.py
│   ├── test_sentiment_crew.py
│   └── test_workflow.py
│
├── run_adk_web.bat           # Start all services
├── stop_servers.bat          # Stop all services
├── docker-compose.yml
└── pyproject.toml
```

## Configuration

Key environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `ADK_MODEL` | `openai/gpt-oss-20b` | LLM model for the orchestrator |
| `AGENT_SEED_URLS` | `http://localhost:8002,http://localhost:8003,http://localhost:8004` | A2A agent discovery URLs |
| `A2A_TIMEOUT` | `300.0` | Timeout for A2A communication (seconds) |
| `LLM_BASE_URL` | `http://localhost:1234/v1` | LM Studio OpenAI-compatible endpoint |

## Testing

```bash
uv run pytest -v
```

42 tests covering: A2A discovery, agent card validation, orchestrator tools, sub-agent executors, LangGraph state graphs, RAG pipelines, CrewAI integration, and workflow state machines.

## License

Apache 2.0
