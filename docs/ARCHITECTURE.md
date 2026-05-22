# Architecture

## Overview

FinSight is a multi-agent investment research system where four specialized agents communicate via the **Google A2A Protocol** (Agent-to-Agent). The orchestrator (ADK `LlmAgent`) discovers sub-agents at startup via `A2ACardResolver`, delegates tasks via a single `send_message` tool, and the LLM routes to each agent sequentially. Each sub-agent processes tasks internally using its own framework and tools.

## Communication Pattern

```
┌──────────────────────────────────────────────────────────────┐
│                    A2A Protocol Layer                         │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Agent Card Discovery                                  │    │
│  │ GET /.well-known/agent-card.json                      │    │
│  │ ─ name, skills (id + description), interfaces         │    │
│  └──────────────────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ JSON-RPC over HTTP (streaming)                        │    │
│  │ POST /a2a  events: task, status_update, artifact_upd  │    │
│  │ Headers: A2A-Version: 1.0                             │    │
│  └──────────────────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Task Lifecycle                                        │    │
│  │ SUBMITTED → WORKING → (artifacts) → COMPLETED        │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

## Orchestrator Architecture

The orchestrator (`agent_1_adk/`) uses a single `LlmAgent` with one `send_message` tool that delegates to sub-agents via A2A:

```
Module load → SubAgentClient.discover()
  ├── A2ACardResolver(httpx.AsyncClient, url) per seed URL
  ├── Returns typed AgentCard (protobuf)
  └── self.agents populated → instruction updated

A2A Request → FinSightAgentExecutor.execute()
  → RUNNER.run_async(user_query)
  → LlmAgent (no pre-fetch)
    → LLM calls send_message(agent_name, task) for each agent
    → SubAgentClient → A2A task to sub-agent
    → SubAgentClient → A2A response (text or data parts)
    → LLM calls next agent
    → LLM synthesizes BUY/HOLD/SELL
  → COMPLETED with synthesis
```

**Key design choices:**

- **A2ACardResolver for discovery** — standard A2A well-known endpoint, proper protobuf AgentCard types, compatibility with legacy formats
- **ClientFactory for A2A communication** — matches the official A2A SDK pattern, supports multiple transport protocols
- **Single `send_message` tool** — not one tool per agent. LLM passes agent name as a parameter, matching all A2A sample projects
- **Streaming event handling** — correctly skips intermediate SUBMITTED/WORKING events, captures `artifact_update` (data/text) and terminal `status_update` events
- **Background async discovery** — supports both ADK Web UI (running event loop) and CLI entrypoints
- **WindowsSelectorEventLoopPolicy** — prevents ConnectionResetError noise on Windows
- **Programmatic AgentCards** — built in code using protobuf `AgentCard(...)` instead of static JSON files

## GenericAgentExecutor Pattern

All sub-agents extend `BaseAgent` and implement `stream()`. A shared `GenericAgentExecutor` handles A2A lifecycle:

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor.execute()
  → BaseAgent.stream(query, context_id, task_id)
  → Yields: {response_type, content, is_task_complete, require_user_input}
  → GenericAgentExecutor converts to TaskStatusUpdateEvent / TaskArtifactUpdateEvent
  → COMPLETED state
```

## Agent Architecture

### RAG Agent (LlamaIndex)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(RAGAgent)
  → RAGAgent.stream()
    → RAGAgent._ensure_ingested(ticker)
      ├── MCPClient.connect_all()
      ├── MCP: get_company_filings(ticker) → returns filings with edgar_url + ix_url
      ├── MCP: get_filing_content(edgar_url, ix_url) for each filing → raw text
      └── DocumentIngestionPipeline.ingest_sec_filings_batch() → ChromaDB
    → FinancialIndexManager.query(ticker, query)
      ├── Try: RouterQueryEngine
      └── Fallback: SEC filings index directly
  → Yields data response with summary + sources
```

**Content Ingestion**: The RAG agent now fetches actual SEC filing content (10-K, 10-Q, 8-K) via `get_filing_content()`, which extracts text from raw EDGAR URLs with fallback to IXBRL viewer URLs. This enables the RAG index to contain actual filing text rather than just metadata.

### Quant Agent (LangGraph)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(QuantAgent)
  → QuantAgent.stream()
    → extract_holdings(query) → portfolio_holdings list
    → analyze(ticker, portfolio_holdings=holdings)
      [MCP: get_prices → parse Close data]
      → compute_metrics → conditional branch (logged with ticker + volatility):
          high volatility (vol > 35%) → stress_test, dcf_error set
          low volatility  (vol ≤ 35%) → dcf_valuation [MCP: get_financials → cash_flow]
      → portfolio_correlation [MCP: get_prices per holding + target ticker]
      → format_output (dcf_error included in reasoning if DCF skipped)
      → llm_summary
  → Yields data response with dcf_error in result
```

**Portfolio Holdings Extraction**: `stream()` uses `extract_holdings(query, exclude_ticker=ticker)` from `shared/ticker_utils.py` to extract holdings from natural language (e.g. "My portfolio holds AAPL, MSFT, GOOGL"). Holdings are passed through the full chain: `stream()` → `analyze()` → `graph.run()` → `correlation_node`.

**Correlation only on explicit request**: The orchestrator prompt instructs the LLM to include holdings in the quant agent task only when the user explicitly mentions portfolio holdings or asks for correlation in their current message. Memory context portfolio lines are labelled as background reference so the LLM does not auto-include them for every single-ticker query.

**Correlation Matrix Notes**: When no holdings are provided, returns `{"note": "No portfolio holdings provided..."}` instead of `{}`. When price data is insufficient or computation fails, returns a descriptive error.

**DCF Fix**: The DCF valuation now correctly reads free cash flow data from the `cash_flow` financial statement (not `income_statement`). This fixes the issue where DCF valuations were returning null.

**DCF Skip Messaging**: When annual volatility exceeds the 35% threshold, `compute_metrics_node` sets a descriptive `dcf_error` field (e.g. "DCF skipped: annual volatility (41.0%) exceeds 35% threshold – routed to stress test instead"). This error is propagated through the graph into the final output and LLM reasoning, providing visibility into why DCF was not computed.

### Sentiment Agent (CrewAI)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(SentimentAgent)
  → SentimentAgent.stream()
    → Parallel MCP data collection (asyncio.gather):
      ├── get_news_sentiment
      └── get_company_filings
    → 2-agent CrewAI: Analysis → Synthesis
  → Yields data response
```

## MCP Architecture

Single unified MCP server (`mcp_servers/finsight_server.py`, port 8010) hosting agent registry + all data tools:

```
┌──────────────────────────────────────────────────────┐
│              finsight-mcp (port 8010)                 │
│                                                       │
│  Agent Registry         │  Data Sources                    │
│  ├── find_agent()       │  ├── get_prices()                │
│  ├── resource://cards   │  ├── get_financials()            │
│  └── resource://{name}  │  ├── get_options_chain()         │
│                         │  ├── get_company_filings()       │
│                         │  ├── get_financial_filings()     │
│                         │  ├── get_filing_content()        │
│                         │  ├── validate_ticker()           │
│                         │  ├── resolve_company_ticker()    │
│                         │  ├── full_text_search()          │
│                         │  ├── get_news_sentiment()        │
│                         │  ├── get_earnings_calendar()     │
│                         │  └── execute_python()            │
└──────────────────────────────────────────────────────┘
```

Agent cards loaded from `agent_cards/*.json`, embedded via `sentence-transformers`, queried via `find_agent` tool using dot-product similarity.

## Timeout Architecture

Timeouts configured via `.env` with `A2A_TIMEOUT=300.0`:

| Layer | Timeout | Mechanism |
|---|---|---|
| A2A discovery | 10s per URL | httpx.AsyncClient within A2ACardResolver |
| A2A messaging | 300s | ClientConfig + httpx.AsyncClient |
| MCP tool calls | 30s | MCPClient default |

## Error Handling

| Failure Mode | Strategy |
|---|---|
| Agent discovery failure | Retries 3x with 5s delay |
| A2A timeout | Caught, returns error JSON to LLM |
| MCP connection failure | Exponential backoff (2^attempt), max 3 retries |
| Agent unavailable | Skipped, LLM works with what it has |
| Response parse failure | `json.JSONDecodeError` caught, text used as-is |

## LLM Configuration

All agents use LM Studio (OpenAI-compatible local API):

| Agent | Model | Provider |
|---|---|---|
| Orchestrator (ADK) | `qwen/qwen3-30b-a3b-2507` | `openai/` prefix (LM Studio endpoint) |
| RAG (LlamaIndex) | `qwen/qwen3-30b-a3b-2507` | `llama-index-llms-openai-like` |
| Quant (LangGraph) | `qwen/qwen3-30b-a3b-2507` | `langchain-openai` |
| Sentiment (CrewAI) | `qwen/qwen3-30b-a3b-2507` | CrewLLM (OpenAI-compatible) |

## Observability & Tracing

Langfuse provides distributed tracing across all four agent processes. Trace context is propagated through the A2A protocol via text-based injection.

### Trace Propagation Flow

```
Orchestrator (agent_1_adk)
  ├── agent_executor.py: langfuse.trace(name="finsight-query")
  ├── start_as_current_observation(name="orchestrator-execute")
  ├── send_message() tool: get_current_trace_id() + get_current_observation_id()
  └── inject_trace_context(task, trace_id, parent_span_id) → A2A text prefix

A2A Protocol (JSON-RPC over HTTP)
  └── Task text prefixed with {"_trace": {"trace_id": "...", "parent_span_id": "..."}}

Sub-agents (agent_2, agent_3, agent_4)
  ├── extract_trace_ids(query) → (trace_id, parent_span_id, clean_query)
  ├── start_observation(trace_context={"trace_id": ..., "parent_span_id": ...})
  └── Langfuse joins the span to the orchestrator's trace tree
```

### Trace Context Utility

| Function | File | Purpose |
|---|---|---|
| `inject_trace_context(task, trace_id, parent_span_id)` | `shared/trace_context.py` | Serializes trace IDs as JSON prefix in task text |
| `extract_trace_context(task)` | `shared/trace_context.py` | Returns `(trace_ctx_dict, clean_query)` |
| `extract_trace_ids(task)` | `shared/trace_context.py` | Returns `(trace_id, parent_span_id, clean_query)` |

### Span Noise Filtering

The Langfuse client uses `is_default_export_span` to filter out noisy A2A internal spans (`a2a-python-sdk` scope) and HTTPX transport spans. Only high-level workflow spans (`langfuse-sdk` scope) and LLM spans are exported, keeping traces clean and focused.

### Per-Agent Instrumentation

| Agent | Service Name | Instrumentors | Manual Spans |
|---|---|---|---|
| Orchestrator | `orchestrator` | GoogleADKInstrumentor, HTTPXClientInstrumentor | `orchestrator-execute` span |
| RAG Agent | `rag_agent` | LlamaIndexInstrumentor | `rag-agent-stream` span |
| Quant Agent | `quant_agent` | StarletteInstrumentor | `quant-agent-stream` span + CallbackHandler for LangGraph nodes |
| Sentiment Agent | `sentiment_agent` | CrewAIInstrumentor, StarletteInstrumentor | `sentiment-agent-stream` span |
| MCP Server | `mcp_server` | — | `@observe()` on individual tools |

## Memory Layer Architecture

The memory layer provides persistent session storage and cross-session memory retrieval using SQLite. All memory components are in `shared/memory/`.

### Overview

```
┌──────────────────────────────────────────────────────────────┐
│                    Memory Layer (SQLite)                       │
│                                                               │
│  ┌─────────────────┐  ┌─────────────────┐  ┌───────────────┐ │
│  │ Session Store   │  │ Ticker Memory   │  │ Portfolio     │ │
│  │ (ADK native)    │  │ (briefs/recs)   │  │ Store         │ │
│  │ DatabaseSession │  │ format_context()│  │ holdings/risk │ │
│  └─────────────────┘  └─────────────────┘  └───────────────┘ │
│                                                               │
│  ┌─────────────────┐  ┌─────────────────┐                    │
│  │ Performance     │  │ Memory Service  │                    │
│  │ Tracker         │  │ (load_memory)   │                    │
│  │ accuracy stats  │  │ cross-session   │                    │
│  └─────────────────┘  └─────────────────┘                    │
└──────────────────────────────────────────────────────────────┘
```

### Session Persistence

`DatabaseSessionService` (ADK native) replaces `InMemorySessionService`:

```python
DatabaseSessionService(db_url="sqlite+aiosqlite:///./finsight_memory.db")
```

All conversation events (user messages, agent responses, tool calls) are persisted to SQLite tables (`sessions`, `events`). Conversations survive server restarts.

### Memory Context Injection

Before each query, the executor injects memory context into the user message:

```
User Query → Executor._inject_memory_context(query)
  ├── extract_ticker(query) → "NVDA"
  ├── TickerMemory.get_latest("NVDA") → last recommendation
  ├── PortfolioStore.get() → current holdings
  └── Prepend: [MEMORY CONTEXT] ... [/MEMORY CONTEXT]
       → Runner receives augmented query
```

The memory context is compact (~300 tokens) and includes:
- Latest recommendation for the queried ticker (if exists)
- Current portfolio holdings (labelled as background reference — not forwarded to sub-agents unless user explicitly requests portfolio analysis)
- Timestamp of last interaction

### Component Architecture

| Component | File | Purpose |
|---|---|---|
| `SQLiteStore` | `shared/memory/store.py` | SQLite connection, auto-migration, table creation |
| `TickerMemory` | `shared/memory/ticker_memory.py` | Per-ticker brief storage, `format_context()` for prompt injection |
| `PortfolioStore` | `shared/memory/portfolio_store.py` | User profile, holdings persistence, risk profile |
| `PerformanceTracker` | `shared/memory/performance_tracker.py` | Recommendation outcome tracking, accuracy evaluation |
| `SQLiteMemoryService` | `shared/memory/memory_service.py` | ADK `BaseMemoryService` implementation for `load_memory` tool |

## File Logging

All services write structured logs to the `logs/` directory via `shared/logging_config.py`:

```python
from shared.logging_config import setup_file_logging
setup_file_logging("orchestrator")  # → logs/orchestrator.log
```

`setup_file_logging(service_name)` attaches a `RotatingFileHandler` (10 MB max, 5 backups) and a `StreamHandler` to the root logger. It is called at module level in each server entry point so logging is configured whether the process is started via uvicorn or run directly. Duplicate handler registration is guarded.

| Service | Log file |
|---|---|
| Orchestrator | `logs/orchestrator.log` |
| RAG Agent | `logs/rag_agent.log` |
| Quant Agent | `logs/quant.log` |
| Sentiment Agent | `logs/sentiment.log` |
| MCP Server | `logs/mcp.log` |
| Memory callback | `logs/memory_callback.log` |

### SQLite Schema

```sql
sessions (id, user_id, created_at, updated_at)
events (id, session_id, event_type, data, created_at)
ticker_briefs (id, ticker, recommendation, confidence, response_text, created_at)
user_profiles (id, user_id, holdings_json, risk_profile, investment_horizon, updated_at)
recommendation_records (id, ticker, recommendation, confidence, price_at_recommendation, created_at)
memory_entries (id, session_id, content_hash, content, created_at)
```

### Auto-Save Flow

After each successful response, the executor automatically persists:

```
Response Complete → Executor._auto_save_memory(query, response)
  ├── extract_ticker(query) → "NVDA"
  ├── TickerMemory.store_brief(ticker, recommendation, response)
  ├── PortfolioStore.update_holdings(extracted_holdings)
  └── PerformanceTracker.record_recommendation(ticker, rec, confidence)
```

The LLM does not need to call any tool to persist memory — auto-save happens on every response.

### Memory Search

The `load_memory` tool is available to the orchestrator LLM for searching past conversations:

```python
# ADK tool injection
memory_service = SQLiteMemoryService(store)
runner = Runner(
    agent=agent,
    session_service=session_service,
    memory_service=memory_service,  # Enables load_memory tool
)
```

The LLM can call `load_memory(query="What did I ask about NVDA last week?")` to search across all past sessions.

### Integration Points

| File | Integration |
|---|---|
| `agent_1_adk/main.py` | Initializes `DatabaseSessionService` and `SQLiteMemoryService` |
| `agent_1_adk/agent_executor.py` | Memory context injection, auto-save, `_add_to_memory` |
| `agent_1_adk/agent.py` | System prompt includes memory usage instructions |

### Database File

- Location: `finsight_memory.db` at project root
- Excluded from git via `.gitignore`
- Auto-created on first use with schema migration
