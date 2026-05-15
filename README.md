# FinSight — Multi-Agent Investment Research System

An autonomous multi-agent system that answers investment queries like *"Should I invest in NVIDIA?"* by coordinating three specialized agents across different frameworks, communicating via the **Agent-to-Agent (A2A)** protocol, and using **MCP (Model Context Protocol)** servers for external tool access.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│              ADK Web UI (port 8001)                           │
│           Orchestrator (ADK LlmAgent)                        │
│         Discovers agents → generates tools → delegates       │
│         Each A2A agent = one ADK tool                        │
└──────────────────────┬───────────────────────────────────────┘
                       │ A2A Protocol (JSON-RPC over HTTP)
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
| **Orchestrator** | Google ADK | 8001 | `agent_1_adk/agent_card.json` | ADK `LlmAgent` with tool-per-agent |
| **RAG** | LlamaIndex + ChromaDB | 8002 | `agent_cards/rag_agent.json` | `GenericAgentExecutor(RAGAgent)` |
| **Quant** | LangGraph + MCP | 8003 | `agent_cards/quant_agent.json` | `GenericAgentExecutor(QuantAgent)` |
| **Sentiment** | CrewAI | 8004 | `agent_cards/sentiment_agent.json` | `GenericAgentExecutor(SentimentAgent)` |
| **MCP Server** | FastMCP | 8010 | `mcp_servers/finsight_server.py` | Registry + all data tools |

### How the Orchestrator Works

The orchestrator uses ADK's `SequentialAgent` + `ParallelAgent` to run sub-agents concurrently:

1. **Discovers agents at startup** — Fetches agent cards from seed URLs via sync HTTP (no asyncio conflicts with ADK Web UI). Retries up to 3 times for slow-starting agents.
2. **Runs all sub-agents in parallel** — A `ParallelAgent` fans out A2A calls to all discovered agents simultaneously.
3. **Synthesizes results** — A final `LlmAgent` collects all agent responses and produces a BUY/HOLD/SELL recommendation.

A2A clients for communicating with sub-agents are created lazily on first call, ensuring they use the correct async event loop.

## Tech Stack

| Layer | Technology |
|---|---|
| Agent Communication | Google A2A Protocol (JSON-RPC over HTTP) |
| Orchestrator | Google ADK `SequentialAgent` + `ParallelAgent` |
| Sub-agent Executor | `GenericAgentExecutor` + `BaseAgent` pattern |
| RAG | LlamaIndex + ChromaDB (local) + HuggingFace embeddings |
| Quant | LangChain + LangGraph (state machine, MCP data) |
| Sentiment | CrewAI (parallel data collection + synthesis) |
| MCP Server | FastMCP (agent registry + data tools) |
| LLM | LM Studio (local, OpenAI-compatible) |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2, local) |
| Vector Store | ChromaDB (local, persisted) |
| Agent Discovery | Seed URLs (AGENT_SEED_URLS) + MCP registry |

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

**Startup order:** MCP Server → RAG → Quant → Sentiment → ADK Web UI

Open http://127.0.0.1:8001 in your browser.

### Stop All Services

```bat
stop_servers.bat
```

## Project Structure

```
├── agent_1_adk/              # ADK Orchestrator
│   ├── agent.py              # LlmAgent with dynamic per-agent tools
│   ├── agent_executor.py     # FinSightAgentExecutor (A2A server runtime)
│   ├── sub_agent_client.py   # SubAgentClient — sync discovery + lazy A2A client
│   ├── main.py               # A2A server entrypoint (click + uvicorn)
│   └── agent_card.json       # Orchestrator's A2A agent card
│
├── agent_2_llamaindex/       # RAG Agent
│   ├── server.py             # GenericAgentExecutor(RAGAgent)
│   ├── executor.py           # RAGAgent extends BaseAgent with stream()
│   ├── index_manager.py      # ChromaDB multi-index + Ollama LLM
│   ├── hybrid_search.py      # BM25 + dense + RRF + reranker
│   └── document_ingestion.py # MCP ingestion pipeline
│
├── agent_3_langgraph/        # Quant Agent
│   ├── server.py             # GenericAgentExecutor(QuantAgent)
│   ├── executor.py           # QuantAgent extends BaseAgent with stream()
│   ├── graph.py              # LangGraph state machine
│   ├── nodes.py              # Nodes + Ollama LLM summary
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
├── agent_cards/              # Declarative A2A Agent Card JSON files
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
├── tests/                    # Test suite (40 tests)
│   ├── test_a2a_communication.py
│   ├── test_agent_cards.py
│   ├── test_base_agent.py
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

## License

Apache 2.0
