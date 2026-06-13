# FinSight — Multi-Agent Investment Research System

An autonomous multi-agent system that answers investment queries like *"Should I invest in NVIDIA?"* by coordinating four specialized agents across different frameworks, communicating via the **Agent-to-Agent (A2A)** protocol, and using **MCP (Model Context Protocol)** servers for external tool access.

## Key Features

- **Multi-framework orchestration**: Google ADK orchestrator delegates to LlamaIndex (RAG), LangGraph (Quant), and CrewAI (Market Context) agents
- **A2A protocol**: Standard-compliant agent discovery and streaming communication via JSON-RPC over HTTP
- **Multi-tier caching**: TTL-based tool-result cache in the MCP server (1 min prices, 1 h financials, 5 min news, permanent filings, 24h peers, 7d scenario shocks), LangChain SQLiteCache for LLM responses, semantic cache using ChromaDB cosine similarity, and LLM priority queue (`CRITICAL`/`NORMAL`/`LOW`) to prevent eval starvation of production inference
- **Investment deck generation**: PPTX, DOCX, self-contained HTML, and PDF reports generated from any stored brief via `generate_pptx()`, `generate_docx()`, `generate_html()`, `generate_pdf_async()` — served at `/api/reports/{brief_id}/{format}` and `/api/reports/ticker/{symbol}/latest/{format}`. PPTX generation tries Playwright first (screenshot-based) before falling back to python-pptx.
- **AG-UI / CopilotKit frontend**: Next.js 16 + CopilotKit 1.59 streaming chat interface (`src/web/nextjs-app/`) via `POST /a2a-agui`; AG-UI bridge with off-topic guardrail, brief auto-save, per-event timeout, and `active_agents` state tracking
- **Input/output guardrails**: Off-topic filter, pre-flight ticker validation, empty-response guard, and BUY/HOLD/SELL signal enforcement with auto-retry
- **Persistent memory layer**: SQLite-backed session storage, cross-session memory search, ticker brief history, portfolio persistence, and recommendation tracking with live price snapshots
- **Incremental RAG ingestion**: Tracks ingested filing URLs in SQLite — restarts never re-ingest already-indexed documents
- **RAGAS evaluation pipeline**: Offline batch evaluation (Faithfulness, ResponseRelevancy, ContextPrecision, ContextRecall, ToolCallAccuracy, AgentGoalAccuracy) with Langfuse score push. Runtime per-query evaluation on live production responses with per-metric streaming, client caching, and 180s LLM timeout
- **Structured logging**: `@logged`/`@logged_sync` timing decorators emit `Enter`/`Exit`/`Fail` with `latency_ms`; operational log statements at cache, DB, sandbox, and report-generation boundaries; noisy third-party loggers suppressed by default, overridable via `LOG_LEVEL_<LIB>` env vars
- **Portfolio correlation analysis**: When you explicitly mention portfolio holdings (e.g. "My portfolio holds AAPL, MSFT"), the quant agent computes cross-stock correlation matrices alongside the primary analysis
- **Distributed tracing**: Langfuse traces span all four agent processes in a single trace tree via text-based context propagation, with automatic filtering of noisy A2A internal spans
- **Health monitoring**: `/health` endpoints on all five services with docker-compose healthcheck integration
- **Local LLM inference**: All agents use LM Studio (OpenAI-compatible API) — no cloud dependencies
- **MCP data tools**: Unified server providing SEC filings, price data, financials, news sentiment, insider transactions, peer discovery, scenario shocks, and more

## Architecture

```
+--------------------------------------------------------------+
│              ADK Web UI (port 8080)                           │
│           Orchestrator (ADK LlmAgent)                        │
│         Discovers agents ? LLM routes via send_message       │
│         Tools: send_message(name, task), load_memory(query)  │
+--------------------------------------------------------------+
                       │ A2A Protocol (JSON-RPC over HTTP, streaming)
                       ?
+--------------------------------------------------------------+
│  Agent Pool                                                   │
│  RAG (:8002)    Quant (:8003)    Market Context (:8004)      │
│  (LlamaIndex)   (LangGraph)      (CrewAI)                    │
+--------------------------------------------------------------+
         │            │                │
         ?            ?                ?
+--------------------------------------------------------------+
│          Unified finsight-mcp Server (port 8010)              │
│  +-----------------+  +---------------------------------+   │
│  │ Agent Registry  │  │  Data Sources                 │   │
│  │ find_agent()    │  │  get_prices, get_financials,  │   │
│  │ resource://agent_cards│  │  get_options_chain,           │   │
│  +-----------------+  │  get_company_filings,         │   │
│                        │  get_financial_filings,       │   │
│                        │  get_filing_content,          │   │
│                        │  validate_ticker,             │   │
│                        │  resolve_company_ticker,      │   │
│                        │  full_text_search,            │   │
│                        │  get_news_sentiment,          │   │
│                        │  get_earnings_calendar,       │   │
│                        │  get_insider_transactions,    │   │
│                        │  get_peers,                   │   │
│                        │  get_macro_indicators,        │   │
│                        │  get_scenario_shocks,         │   │
│                        │  execute_python, ...          │   │
│                        +---------------------------------+   │
+--------------------------------------------------------------+
```

All A2A communication uses `A2ACardResolver` for standard discovery and `ClientFactory` for transport. Streaming events are handled correctly: intermediate `WORKING`/`SUBMITTED` events are skipped, only actual results (`artifact_update` data or terminal `COMPLETED` status) are returned.

## Tech Stack

| Layer | Technology |
|---|---|
| Agent Communication | Google A2A Protocol (JSON-RPC over HTTP, streaming) |
| Orchestrator | Google ADK `LlmAgent` with `send_message` tool |
| Sub-agent Executor | `GenericAgentExecutor` + `BaseAgent` pattern |
| Memory Layer | SQLite (`aiosqlite`) — sessions, ticker briefs, portfolio, performance, ingested filings |
| Caching | `_TTLCache` (MCP tools), LangChain `SQLiteCache` (LLM), ChromaDB semantic cache, `LLMPriorityQueue` (async semaphore, 3 tiers) |
| Guardrails | Regex off-topic filter + MCP ticker pre-check (input), signal check + retry (output) |
| RAG | LlamaIndex + ChromaDB (local) + HuggingFace embeddings, incremental ingestion |
| Quant | LangChain + LangGraph (state machine, MCP data) + LangChain SQLiteCache |
| Market Context | CrewAI (macro regime + peer landscape synthesis) |
| MCP Server | FastMCP (agent registry + data tools + TTL caching) |
| Report Generator | `src/shared/reports/` package — python-pptx (PPTX), python-docx (DOCX), Jinja2 (HTML), Playwright (PDF + screenshot PPTX) |
| Frontend | Next.js 16 + CopilotKit 1.59 + @ag-ui/client (`src/web/nextjs-app/`) via `POST /a2a-agui` |
| Evaluation | RAGAS offline pipeline + runtime per-query eval (per-metric streaming, client caching, 180s timeout) + custom financial rubrics |
| LLM | LM Studio (local, OpenAI-compatible) |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2, local) |
| Reranker | cross-encoder/ms-marco-MiniLM-L-6-v2 |
| Vector Store | ChromaDB (local, persisted) |
| Agent Discovery | `A2ACardResolver` via `AGENT_SEED_URLS` |
| Observability | Langfuse + LangChainInstrumentor + sub-agent latency spans |
| Logging | `src/shared/logging_config.py` — `@logged`/`@logged_sync` decorators, third-party suppression, `LOG_LEVEL_<LIB>` overrides |

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- [LM Studio](https://lmstudio.ai) with a model loaded (e.g. `ministral-3-14b-reasoning` or `qwen3-30b-a3b-2507`) on port 1234

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
uv run python -m uvicorn mcp_tools.finsight_server:get_app --host 0.0.0.0 --port 8010

# Terminal 2: RAG Agent
uv run python -m uvicorn financial_rag.server:app --host 0.0.0.0 --port 8002

# Terminal 3: Quant Agent
uv run python -m uvicorn quant.server:app --host 0.0.0.0 --port 8003

# Terminal 4: Market Context Agent
uv run python -m uvicorn market_context.server:app --host 0.0.0.0 --port 8004

# Terminal 5: ADK Web UI
uv run adk web --port 8080 --session_service_uri sqlite://./db/adk_sessions.db --memory_service_uri finsight:// agents
```

**Startup order:** LM Studio ? MCP Server ? RAG ? Quant ? Market Context ? ADK Web UI

Open http://127.0.0.1:8080 in your browser.

> The orchestrator's standalone A2A server (`src/orchestrator/main.py` on `:8001`) is no longer started by `run_adk_web.bat`. The orchestrator runs inside `adk web`. Start it manually with `uv run python -m orchestrator.main` if you need to expose the A2A JSON-RPC endpoint to external A2A clients.

### Stop All Services

```bat
stop_servers.bat
```

## Project Structure

```
+-- src/
│   +-- orchestrator/          # ADK Orchestrator
│   │   +-- agent.py           # LlmAgent with single send_message tool
│   │   +-- agent_executor.py  # FinSightAgentExecutor (guardrails, semantic cache, A2A runtime)
│   │   +-- sub_agent_client.py# SubAgentClient (A2A discovery + latency tracking)
│   │   +-- main.py            # A2A server entrypoint (uvicorn)
│   │   +-- web/               # ADK Web callbacks (merged from agents/)
│   │   +-- Dockerfile
│   │
│   +-- financial_rag/          # RAG Agent
│   │   +-- server.py           # GenericAgentExecutor(RAGAgent)
│   │   +-- executor.py         # RAGAgent extends BaseAgent with stream()
│   │   +-- index_manager.py    # ChromaDB multi-index + LM Studio LLM
│   │   +-- hybrid_search.py    # BM25 + dense + RRF + reranker
│   │   +-- document_ingestion.py
│   │
│   +-- quant/                  # Quant Agent
│   │   +-- server.py           # GenericAgentExecutor(QuantAgent)
│   │   +-- executor.py         # QuantAgent extends BaseAgent with stream()
│   │   +-- graph.py            # LangGraph state machine
│   │   +-- nodes/              # Compute nodes (calculations, dcf, etc.)
│   │   +-- state.py            # QuantAnalysisState schema
│   │
│   +-- market_context/         # Market Context Agent
│   │   +-- server.py           # GenericAgentExecutor(MarketContextAgent)
│   │   +-- executor.py         # MarketContextAgent extends BaseAgent with stream()
│   │   +-- crew.py             # MarketContextCrew (macro regime + peer landscape)
│   │   +-- mcp_tools.py        # DynamicMCPTool with Pydantic args_schema
│   │
│   +-- mcp_tools/              # MCP Server (port 8010)
│   │   +-- finsight_server.py  # get_app() (FastMCP)
│   │   +-- tools/              # Per-tool modules (market_data, edgar, sentiment, etc.)
│   │   +-- infra/              # Rate limiters, caching, embed loader
│   │
│   +-- shared/                 # Shared libraries
│   │   +-- base_agent.py       # BaseAgent abstract class
│   │   +-- settings.py         # Pydantic-settings BaseSettings
│   │   +-- bootstrap.py        # Process-level side-effects
│   │   +-- mcp_client.py       # MCP client with dynamic tool discovery
│   │   +-- reports/            # HTML/PPTX/DOCX report generation
│   │   +-- memory/             # SQLite persistence layer
│   │   +-- templates/          # Jinja2 templates
│   │
│   +-- web/nextjs-app/         # Next.js 16 + CopilotKit 1.59 frontend
│   +-- tests/                  # Unit, characterization, regression, security tests
│   +-- scripts/                # Utility scripts
│
+-- agent_cards/               # A2A Agent Card JSON files
+-- db/                        # SQLite + ChromaDB data
+-- docs/                      # Documentation
+-- deploy/                    # Deployment configs
+-- run_ui.bat                 # Start all services (AG-UI mode)
+-- run_adk_web.bat            # Start all services (ADK web mode)
+-- docker-compose.yml
+-- pyproject.toml
```

## Configuration

Key environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `ADK_MODEL` | `openai/mistralai/ministral-3-14b-reasoning` | LLM model for the orchestrator (default `openai/qwen/qwen3-30b-a3b-2507` in `settings.py`) |
| `AGENT_SEED_URLS` | `http://localhost:8002,http://localhost:8003,http://localhost:8004` | A2A agent discovery URLs |
| `A2A_TIMEOUT` | `680.0` | Timeout for A2A communication (seconds) |
| `LLM_BASE_URL` | `http://localhost:1234/v1` | LM Studio OpenAI-compatible endpoint |
| `LLM_API_KEY` | `lmstudio` | API key for LLM provider (LM Studio dummy value; replace for OpenAI/Anthropic) |
| `SEC_USER_AGENT` | `FinSight Research (dev-mode-set-SEC_USER_AGENT)` | SEC EDGAR User-Agent header (format: `Your Name (your-email@example.com)`) |
| `SEMANTIC_CACHE_ENABLED` | `false` | Enable ChromaDB semantic cache for repeated investment queries |
| `EVAL_TRACE_ENABLED` | `True` | Master switch for sidecar RAGAS evals. Set to `False` to disable all per-agent runtime scoring with no code changes |
| `MCP_SERVER_URL` | `http://localhost:8010/sse` | Unified MCP server SSE endpoint |
| `LOG_LEVEL` | `INFO` | Root log level for all FinSight services |
| `LOG_LEVEL_<LIB>` | `WARNING` | Per-library override (e.g. `LOG_LEVEL_HTTPX=DEBUG`, `LOG_LEVEL_CHROMADB=INFO`) |

## Documentation

| Document | Description |
|---|---|
| `docs/ARCHITECTURE.md` | System architecture, communication patterns, caching layer, guardrails, agent internals |
| `docs/AGENTS.md` | Detailed agent reference (skills, architecture, streaming flow) |
| `docs/API_REFERENCE.md` | Complete endpoint reference — REST routes, A2A protocol, AG-UI, health checks |
| `docs/MCP_SERVERS.md` | MCP server tools, TTL caching, registry, client usage |
| `docs/DESIGN_DECISIONS.md` | Evolution log: why each design choice was made |
| `docs/CHANGELOG.md` | Version history |
| `docs/TESTS.md` | Test coverage, patterns, RAGAS evaluation, running instructions |
| `docs/SECURITY.md` | Auth model, Python sandbox, trusted proxies, hardening history |
| `src/web/nextjs-app/README.md` | Frontend architecture — pages, components, design system, data flow |

## Testing

**~317 parametrized test cases** across 28 test files — see [TESTS.md](docs/TESTS.md) for details.

## License

Apache 2.0
