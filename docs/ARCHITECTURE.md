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
      ├── Filter: is_filing_ingested(edgar_url) — skip already-indexed URLs
      ├── MCP: get_filing_content(edgar_url, ix_url) for each new filing → raw text
      ├── DocumentIngestionPipeline.ingest_sec_filings_batch() → ChromaDB
      └── mark_filing_ingested(edgar_url, ticker) for each new filing
    → FinancialIndexManager.query(ticker, query)
      ├── Try: RouterQueryEngine
      └── Fallback: SEC filings index directly
  → Yields data response with summary + sources
```

**Incremental Ingestion**: `_ensure_ingested()` checks the `ingested_filings` SQLite table before fetching any filing content. URLs already indexed in a previous run are skipped entirely — restarts and same-day re-queries do not re-ingest immutable historical filings.

**Pre-warm**: `FinancialIndexManager` is instantiated at server startup in a thread executor via Starlette `on_startup`. The embedding model download is complete before the first A2A request arrives.

**Content Ingestion**: Fetches actual SEC filing content (10-K, 10-Q, 8-K) via `get_filing_content()`, which extracts text from raw EDGAR URLs with fallback to IXBRL viewer URLs.

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
    → 1-agent CrewAI: Analysis → narrative directly
  → Yields data response
```

## Caching Layer

Three independent caching tiers reduce latency and external API load:

### MCP Tool-Result Cache (Tier 1A)

`_TTLCache` in `mcp_servers/finsight_server.py` — `OrderedDict`-backed with `time.monotonic()` expiry, no new dependencies:

| Cache | TTL | Key | Notes |
|---|---|---|---|
| `_cache_prices` | 5 min | `(ticker, period, interval)` | yfinance OHLCV |
| `_cache_financials` | 24 h | `(ticker,)` | income/balance/cashflow |
| `_cache_news` | 15 min | `(ticker, limit)` | only cached when articles found |
| `_cache_filing` | permanent (LRU-200) | `edgar_url` | filings are immutable |
| `_cache_submissions` | 6 h | `cik` | EDGAR CIK submissions |

### LangChain SQLiteCache (Tier 1B)

`agent_3_langgraph/nodes.py` sets `SQLiteCache(database_path=".langchain_cache.db")` before the `ChatOpenAI` instance is used in `llm_summary_node`. Identical ticker+metrics inputs reuse cached LLM output without an LM Studio round-trip.

### Semantic Cache (Tier 1D)

`shared/semantic_cache.py` — ChromaDB collection `finsight_semantic_cache` + `all-MiniLM-L6-v2` embedder (already in-use). Cosine similarity threshold: 0.95; TTL: 1 h; response stored up to 4000 chars in Chroma metadata.

Wired into `agent_1_adk/agent_executor.py`:
- **Before** `runner.run_async`: `SemanticCache.get(query)` — on hit, return immediately
- **After** successful response: `SemanticCache.set(query, text)`
- Controlled by `SEMANTIC_CACHE_ENABLED=true` env var (off by default)

### KV Cache Prefix (Tier 1C)

`agent_1_adk/agent.py` splits `_build_instruction()` into a module-level `_STATIC_PREAMBLE` constant and a dynamic tail (today's date + agent list). LM Studio reuses the KV-cached static prefix across requests. Same pattern applied to CrewAI backstory strings in `agent_4_crewai/crew.py`.

## Guardrails

### Input Guardrails

`agent_1_adk/agent_executor.py`, top of `execute()`:

1. **Off-topic filter** — `_NON_INVESTMENT_RE` regex matches weather/recipes/entertainment/horoscopes. Returns canned message in < 100 ms, no sub-agents invoked.
2. **Ticker pre-check** — when a ticker is extracted, calls MCP `validate_ticker` before spawning sub-agents. Returns clean error in < 2 s if ticker is invalid.
3. **Semantic cache check** — if `SEMANTIC_CACHE_ENABLED`, checks cache before the orchestrator runs.

### Output Guardrails

`agent_1_adk/agent_executor.py`, in `_process_response()`:

1. **Empty/short response guard** — `len(text.strip()) < 50` → `TASK_STATE_FAILED` with structured error.
2. **Signal check** — if response lacks BUY/HOLD/SELL and the query was a stock analysis request, emits a Langfuse warning span with `missing_signal: true`.

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

Timeouts configured via `.env` with `A2A_TIMEOUT=180.0`:

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
| Orchestrator | `orchestrator` | GoogleADKInstrumentor, HTTPXClientInstrumentor | `orchestrator-execute` span, per-sub-agent latency spans |
| RAG Agent | `rag_agent` | LlamaIndexInstrumentor | `rag-agent-stream` span |
| Quant Agent | `quant_agent` | StarletteInstrumentor, **LangChainInstrumentor** | `quant-agent-stream` span + CallbackHandler for LangGraph nodes |
| Sentiment Agent | `sentiment_agent` | CrewAIInstrumentor, StarletteInstrumentor | `sentiment-agent-stream` span |
| MCP Server | `mcp_server` | — | `@observe()` on individual tools |

### Sub-Agent Latency Spans

`agent_1_adk/sub_agent_client.py` wraps each `send_message()` call with a `time.monotonic()` stopwatch and emits a Langfuse span:

```python
lf.observation(
    as_type="span",
    name=f"sub-agent-{agent_name}",
    input={"task": task_str[:200]},
    output={"response": result_text[:200]},
    metadata={"latency_ms": round((t1 - t0) * 1000), "agent": agent_name},
)
```

When `EVAL_TRACE_ENABLED=true`, the same call also writes a JSON file to `tests/evaluation/eval_results/orchestrator_traces/` for use by the RAGAS orchestrator evaluation runner.

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

## Health Endpoints

All five services expose `GET /health`:

| Service | URL | Response |
|---|---|---|
| Orchestrator | `http://localhost:8001/health` | `{"status":"ok","agent":"orchestrator"}` |
| RAG Agent | `http://localhost:8002/health` | `{"status":"ok","agent":"rag"}` |
| Quant Agent | `http://localhost:8003/health` | `{"status":"ok","agent":"quant"}` |
| Sentiment Agent | `http://localhost:8004/health` | `{"status":"ok","agent":"sentiment"}` |
| MCP Server | `http://localhost:8010/health` | `{"status":"ok","agent":"mcp"}` |

The MCP server mounts its health route alongside the FastMCP SSE app via a Starlette wrapper in `get_app()`. Docker-compose `healthcheck` blocks use these endpoints with `curl -f`, and `depends_on` is set to `condition: service_healthy`.

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
recommendation_records (id, ticker, recommendation, confidence, price_at_rec, created_at,
                        evaluated_at, realized_return)
memory_entries (id, session_id, content_hash, content, search_text, created_at)
ingested_filings (edgar_url PRIMARY KEY, ticker, ingested_at)  -- v1.18
```

`ingested_filings` tracks which SEC EDGAR document URLs have been indexed into ChromaDB. The RAG agent checks this table before fetching filing content — already-indexed URLs are skipped, preventing redundant ingest on restart.

### HF_HUB_OFFLINE

`shared/config.py` sets `HF_HUB_OFFLINE=1` at import time before any HuggingFace code runs. This prevents network calls to `huggingface.co` when loading `sentence-transformers` or embedding models — models must be cached locally from a prior online run. Set `HF_HUB_OFFLINE=0` in `.env` to re-enable download checks.

## Runtime RAGAS Evaluation

After each agent produces a response, a fire-and-forget background task scores it using RAGAS metrics that require no ground-truth reference. Scores are pushed to Langfuse per-trace (linked by `trace_id`).

### Feature flag

All sidecar evals are gated by `EVAL_TRACE_ENABLED` in `.env` (default `True`). The flag is exposed as `EVAL_ENABLED` in `shared/config.py`; every agent's `asyncio.create_task(_eval_*)` call site checks it. Set `EVAL_TRACE_ENABLED=False` to disable all per-agent runtime scoring with no code changes — useful for fast iteration when LM Studio judge calls add 5–180s of background work per query.

### Orchestrator eval hook lives in `after_agent_callback`

When the orchestrator runs through `adk web` (the path `run_adk_web.bat` uses), the ADK Web runner is responsible — `FinSightAgentExecutor` is never invoked. The orchestrator's eval is therefore scheduled from `agents/finsight_agent/agent.py`'s `_persist_memory_callback`, not from `agent_executor.py`. The callback first runs `_is_analysis_turn(session.events)`: if `save_brief` was not called in this turn (e.g. the user only asked "what were my last recommendations?"), both memory persist and eval are skipped to avoid polluting long-term memory with conversational queries.

`FinSightAgentExecutor` still keeps its eval call for completeness — it fires when an A2A client hits `agent_1_adk/main.py` directly. The A2A server is not started by `run_adk_web.bat` by default; start it manually with `uv run python -m agent_1_adk.main` if needed.

| Agent | Background Task | Metrics | Why Each Metric | Data Required |
|---|---|---|---|---|---|
| Orchestrator | `score_response()` | AnswerRelevancy, citation_quality, risk_disclosure, recommendation_clarity, response_completeness | AnswerRelevancy: generic catch-all for response quality. citation_quality: unsubstantiated financial claims are worthless — must cite filing dates/amounts. risk_disclosure: an investment thesis without risk discussion is incomplete. recommendation_clarity: the core output is a BUY/HOLD/SELL signal — ambiguous synthesis fails. response_completeness: must synthesize all 3 analysis types, not just one. | `user_input`, `response` |
| RAG | `score_rag_response()` | Faithfulness, AnswerRelevancy, ContextPrecisionWithoutReference | Faithfulness: prevents hallucinated dates/numbers by verifying claims against retrieved SEC text. ContextPrecisionWithoutReference: flags retrieval drift — when RAG returns irrelevant filings, this drops even if Faithfulness passes. | `user_input`, `response`, `context_texts` (ChromaDB nodes) |
| Quant | `score_quant_response()` | FactualCorrectness, AnswerRelevancy | FactualCorrectness: compares LLM summary numbers (Sharpe, VaR, DCF) against actual computed values — primary failure mode is hallucinated numbers. AnswerRelevancy: generic catch-all. | `user_input`, `response`, `quant_result` (computed metrics dict) |
| Sentiment | `score_sentiment_response()` | AnswerRelevancy, catalyst_identification, insider_signal_discussion, Faithfulness | catalyst_identification: vague "sentiment is positive" without naming the catalyst scores low — must identify specific events. insider_signal_discussion: omitting insider trading patterns misses a key sentiment signal. Faithfulness: verifies narrative is grounded in collected news/filing data, not fabricated. | `user_input`, `response`, `_retrieved_contexts` (news/filing titles) |

### Per-Metric Streaming

Metrics within an agent are run concurrently via `asyncio.wait(FIRST_COMPLETED)` instead of `asyncio.gather`. Each metric score is logged and pushed to Langfuse the moment its `ascore()` finishes — fast metrics (AnswerRelevancy, DomainSpecificRubrics ~3-5s) appear immediately without waiting for slow metrics (e.g., Faithfulness which runs multiple sequential LLM calls and can take ~180s).

### Client Caching

`_setup_ragas_clients()` caches the `(InstructorLLM, _STEmbeddings)` tuple at module level after the first call. All four agents reuse the cached `SentenceTransformer` model — the previous approach loaded a fresh model (~1-2s, ~80MB) on every agent response, multiplying latency by 4 per query.

### Error Handling

- **`_score_metric`**: Wraps `metric.ascore()` in try/except with `exc_info=True` logging and re-raises.
- **`_run_metrics`**: Catches `BaseException` (including `CancelledError` which inherits from `BaseException`, not `Exception`) per-metric. Float conversion is guarded with try/except.
- **Scoring functions**: Each function body is wrapped in try/except that logs any unexpected crash with full traceback — prevents fire-and-forget tasks from silently dying.

### Debuggability

Each scoring function logs `[agent] Eval entered` at INFO on entry. Early-return conditions (short response, import failure, no RAGAS clients) log a warning with the reason. The fallback `[agent] No RAGAS scores computed` is at INFO level.

### Timeout & Encoding

The `AsyncOpenAI` client uses a 180-second timeout (up from 60s) — Faithfulness makes multiple sequential LLM calls within a single `ascore()`, and each call can take ~20-30s on a 20B model. `sys.stdout.reconfigure(encoding='utf-8')` at import time prevents `UnicodeEncodeError` from RAGAS log messages with Unicode characters (curly quotes, em-dashes) on Windows cp1252 consoles and file handlers.

### Langfuse Integration

`_push_scores()` pushes each metric to `langfuse.create_score()` linked by `trace_id`. When `trace_id` is None (no active Langfuse trace), the push is skipped entirely to avoid "Bad request" errors from the cloud API with placeholder keys.

**Score namespacing by agent** — each score is pushed as `ragas/{agent}/{metric}` (e.g. `ragas/orchestrator/AnswerRelevancy`, `ragas/rag/Faithfulness`, `ragas/quant/FactualCorrectness`, `ragas/sentiment/catalyst_identification`). The previous flat `ragas/{metric}` naming made it impossible to distinguish the same metric across agents in Langfuse. Each `lf.create_score()` call also carries `comment="agent=<name>"` for additional structured filtering.

### LM Studio Compatibility

RAGAS defaults to `instructor.Mode.JSON` which sends `response_format.type="json_object"` — LM Studio only supports `"json_schema"` or `"text"`. `_setup_ragas_clients()` patches with `instructor.Mode.JSON_SCHEMA`. HuggingFace embeddings are wrapped via a custom `_STEmbeddings` class (RAGAS 0.4.x `BaseRagasEmbedding`) to avoid a broken pydantic integration path.

### Offline-Mode Metrics (Ground-Truth Required)

These metrics require a human-curated reference dataset and live in `tests/evaluation/` as offline scripts:
- `run_rag_eval.py` — Faithfulness, ResponseRelevancy, ContextPrecision, ContextRecall, NoiseSensitivity
- `run_orchestrator_eval.py` — ToolCallAccuracy, AgentGoalAccuracy

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
