# FinSight — Multi-Agent Investment Research System

An autonomous multi-agent system that answers investment queries like *"Should I invest in NVIDIA?"* by coordinating three specialized agents across different frameworks, communicating via the **Agent-to-Agent (A2A)** protocol, and using **MCP (Model Context Protocol)** servers for external tool access.

## Key Features

- **Multi-framework orchestration**: Google ADK orchestrator delegates to LlamaIndex (RAG), LangGraph (Quant), and CrewAI (Sentiment) agents
- **A2A protocol**: Standard-compliant agent discovery and streaming communication via JSON-RPC over HTTP
- **Persistent memory layer**: SQLite-backed session storage, cross-session memory search, ticker brief history, portfolio persistence, and recommendation tracking
- **Portfolio correlation analysis**: Extract holdings from natural language (e.g. "My portfolio holds AAPL, MSFT") and compute cross-stock correlation matrices
- **Distributed tracing**: Langfuse traces span all four agent processes in a single trace tree via text-based context propagation, with automatic filtering of noisy A2A internal spans
- **Local LLM inference**: All agents use LM Studio (OpenAI-compatible API) — no cloud dependencies
- **MCP data tools**: Unified server providing SEC filings, price data, financials, news sentiment, and more

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
│  ┌─────────────────┐  ┌─────────────────────────────────┐   │
│  │ Agent Registry  │  │  Data Sources                 │   │
│  │ find_agent()    │  │  get_prices, get_financials,  │   │
│  │ resource://cards│  │  get_options_chain,           │   │
│  └─────────────────┘  │  get_company_filings,         │   │
│                        │  get_financial_filings,       │   │
│                        │  get_filing_content,          │   │
│                        │  validate_ticker,             │   │
│                        │  resolve_company_ticker,      │   │
│                        │  full_text_search,            │   │
│                        │  get_news_sentiment,          │   │
│                        │  get_earnings_calendar,       │   │
│                        │  execute_python, ...          │   │
│                        └─────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
3. **Synthesizes results** — After all agents respond, the LLM produces a BUY/HOLD/SELL recommendation with supporting evidence.

All A2A communication uses `A2ACardResolver` for standard discovery and `ClientFactory` for transport. Streaming events are handled correctly: intermediate `WORKING`/`SUBMITTED` events are skipped, only actual results (`artifact_update` data or terminal `COMPLETED` status) are returned.

## Tech Stack

| Layer | Technology |
|---|---|
| Agent Communication | Google A2A Protocol (JSON-RPC over HTTP, streaming) |
| Orchestrator | Google ADK `LlmAgent` with `send_message` tool |
| Sub-agent Executor | `GenericAgentExecutor` + `BaseAgent` pattern |
| Memory Layer | SQLite (`aiosqlite`) — sessions, ticker briefs, portfolio, performance |
| RAG | LlamaIndex + ChromaDB (local) + HuggingFace embeddings |
| Quant | LangChain + LangGraph (state machine, MCP data) |
| Sentiment | CrewAI (parallel data collection + synthesis) |
| MCP Server | FastMCP (agent registry + data tools) |
| LLM | LM Studio (local, OpenAI-compatible) |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2, local) |
| Reranker | cross-encoder/ms-marco-MiniLM-L-6-v2 |
| Vector Store | ChromaDB (local, persisted) |
| Agent Discovery | `A2ACardResolver` via `AGENT_SEED_URLS` |

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- [LM Studio](https://lmstudio.ai) with a model loaded (e.g. `qwen3-30b-a3b-2507`) on port 1234

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
│   ├── ticker_utils.py       # Ticker extraction & holdings extraction
│   ├── trace_context.py      # Distributed trace context injection/extraction
│   ├── observability.py      # Langfuse singleton initialization
│   ├── workflow.py           # WorkflowGraph state machine
│   ├── config.py             # Centralized .env configuration
│   ├── mcp_client.py         # MCP client with dynamic tool discovery
│   ├── models.py             # Pydantic data models
│   └── memory/               # Persistent memory layer
│       ├── store.py          # SQLite foundation, auto-migration
│       ├── ticker_memory.py  # Per-ticker brief storage, format_context()
│       ├── portfolio_store.py # User profile, holdings persistence
│       ├── performance_tracker.py # Recommendation outcome tracking
│       ├── memory_service.py # ADK BaseMemoryService (load_memory tool)
│       └── __init__.py       # Exports
│
├── tests/                    # Test suite (72 tests)
│   ├── test_a2a_communication.py
│   ├── test_agent_cards.py
│   ├── test_base_agent.py
│   ├── test_orchestrator_tools.py
│   ├── test_planner.py
│   ├── test_quant_graph.py
│   ├── test_rag_pipeline.py
│   ├── test_sentiment_crew.py
│   ├── test_workflow.py
│   ├── test_trace_propagation.py  # Trace context + holdings extraction
│   └── test_memory.py             # Memory layer (SQLite, ticker, portfolio, performance)
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
| `ADK_MODEL` | `openai/qwen/qwen3-30b-a3b-2507` | LLM model for the orchestrator |
| `AGENT_SEED_URLS` | `http://localhost:8002,http://localhost:8003,http://localhost:8004` | A2A agent discovery URLs |
| `A2A_TIMEOUT` | `300.0` | Timeout for A2A communication (seconds) |
| `LLM_BASE_URL` | `http://localhost:1234/v1` | LM Studio OpenAI-compatible endpoint |

## Documentation

| Document | Description |
|---|---|
| `docs/ARCHITECTURE.md` | System architecture, communication patterns, agent internals |
| `docs/AGENTS.md` | Detailed agent reference (skills, architecture, streaming flow) |
| `docs/MCP_SERVERS.md` | MCP server tools, registry, client usage |
| `docs/DESIGN_DECISIONS.md` | Evolution log: why each design choice was made |
| `docs/DEMO.md` | End-to-end walkthrough with example queries |
| `docs/CHANGELOG.md` | Version history |
| `docs/TESTS.md` | Test coverage, patterns, running instructions |

## Testing

```bash
uv run pytest -v
```

72 tests covering: A2A discovery, agent card validation, orchestrator tools, sub-agent executors, LangGraph state graphs, RAG pipelines, CrewAI integration, workflow state machines, distributed trace propagation, portfolio holdings extraction, and persistent memory layer (SQLite store, ticker briefs, portfolio persistence, performance tracking, cross-session memory search).

## License

Apache 2.0
