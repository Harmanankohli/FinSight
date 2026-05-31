# Architecture

## Overview

FinSight is a multi-agent investment research system where four specialized agents communicate via the **Google A2A Protocol** (Agent-to-Agent). The orchestrator (ADK `LlmAgent`) discovers sub-agents at startup via `A2ACardResolver`, delegates tasks via a single `send_message` tool, and the LLM routes to all agents in parallel (instructed via system prompt to emit all `send_message` calls in one assistant turn). Each sub-agent processes tasks internally using its own framework and tools.

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

The orchestrator (`agent_1_adk/`) uses a single `LlmAgent` with one `send_message` tool that delegates to sub-agents via A2A. Two parallel cache paths can short-circuit the LLM entirely on same-day repeat queries:

```
Module load → SubAgentClient.discover()
  ├── A2ACardResolver(httpx.AsyncClient, url) per seed URL
  ├── Returns typed AgentCard (protobuf)
  └── self.agents populated → instruction updated

A2A Request → FinSightAgentExecutor.execute()
  → _get_today_cached_text(ticker)  — [CACHE: return today brief if exists]
  → _build_memory_context(query)    — inject [MEMORY CONTEXT] with [TODAY]/[STALE] tag
  → RUNNER.run_async(user_query)
    → LlmAgent (no pre-fetch)
    → before_agent_callback: _memory_cache_callback  — [CACHE: return types.Content if today brief exists]
    → LLM emits ALL send_message calls in ONE assistant turn (parallel per system prompt instruction)
    → SubAgentClient → A2A task to sub-agent (parallel)
    → SubAgentClient → A2A response (text or data parts)
    → LLM receives all results together in the next turn
    → LLM synthesizes BUY/HOLD/SELL
  → after_agent_callback: _persist_memory_callback
    → add_events_to_memory()
    → if EVAL_ENABLED: score_response()
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
    → RAGAgent._build_response(query)
      → Fire-and-forget (asyncio.create_task):
        ├── self._ensure_ingested(ticker) — runs in background, non-blocking
        └── self._ensure_news_ingested(ticker) — runs in background, non-blocking
      → FinancialIndexManager.query(ticker, query) — returns from indexed data
        ├── _classify_query_intent() → sec_filings ∪ news ∪ earnings
        ├── Multi-collection retrieval with score-sorted dedup
        └── LlamaIndex response synthesizer (response_mode="compact")
      → If index has no data for this ticker (first query):
        ├── Returns A2A WORKING event with "Index is warming for {ticker}..." message
        ├── Awaits background ingestion to complete
        └── Re-queries index and returns actual data on COMPLETED
  → Yields data response with summary + sources

RAGAgent._ensure_ingested(ticker) [background]:
  → MCPClient.connect_all()
  → MCP: get_company_filings(ticker) → filings with edgar_url + ix_url
  → Filter: is_filing_ingested(edgar_url) — skip already-indexed URLs
  → Parallel fetch via asyncio.gather:
    ├── MCP: get_filing_content(edgar_url, ix_url) for candidate 1
    ├── MCP: get_filing_content(edgar_url, ix_url) for candidate 2
    └── ... (all candidates concurrently, truncated server-side at 25k chars)
  → DocumentIngestionPipeline.ingest_sec_filings_batch() → ChromaDB
  → mark_filing_ingested(edgar_url, ticker) for each new filing
```

**Incremental Ingestion**: `_ensure_ingested()` checks the `ingested_filings` SQLite table before fetching any filing content. URLs already indexed in a previous run are skipped entirely — restarts and same-day re-queries do not re-ingest immutable historical filings.

**Startup warm-up** (`agent_2_llamaindex/server.py`, v1.32): `_do_prewarm()` runs once on Starlette `on_startup` in a thread executor via `asyncio.to_thread`. Three stages: HuggingFace embedder pre-load + dummy encode, three ChromaDB collections (`sec_filings`/`news`/`earnings`) via `get_or_create_collection`, CrossEncoder reranker from `HybridSearchPipeline`. Each stage logs elapsed seconds. Effect: first RAG query pays ~0s model-load tax (was ~3-5s). Warm-up errors are logged but don't crash the server.

**Content Ingestion**: Fetches actual SEC filing content (10-K, 10-Q, 8-K) via `get_filing_content()`, which extracts text from raw EDGAR URLs with fallback to IXBRL viewer URLs.

### Quant Agent (LangGraph)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(QuantAgent)
  → QuantAgent.stream()
    → extract_holdings(query) → portfolio_holdings list
    → analyze(ticker, portfolio_holdings=holdings)
      [Parallel fan-out from START]
        ├── fetch_prices [MCP: get_prices → parse Close data]
        │     → compute_base_metrics (Sharge, vol, VaR, beta)
        │     → technical_analysis (SMA, MACD, RSI, Bollinger, trend)
          │     → volatility gate
          │         ├── high vol (> 35%) → stress_test [sector-aware scenario shocks]
          │         └── low vol (≤ 35%) → dcf_valuation [data-driven WACC + growth]
          │     → monte_carlo (GBM, 5k paths, 252d horizon) — runs in BOTH paths
          ├── fetch_fundamentals [MCP: get_financials → 25+ ratios]
          |     → PE, PB, ROE, margins, D/E, growth, golden cross
          |     → peer_comparison (dynamic yfinance Industry/Sector peers, ranks on PE,
          |         EV/EBITDA, growth, margins, ROE, D/E + sector medians for relative scoring)
          ├── options_flow_node (put/call vol ratio, OI ratio, flow signal, no-data handling)
          ├── insider_signals_node (get_insider_transactions MCP — structured buy/sell data)
          └── analyst_positioning_node (consensus, upside %, short interest, squeeze)
          [Fan-in]
            ├── portfolio_correlation [MCP: get_prices per holding + target]
            └── format_output (8-group weighted voting: risk 0.15, dcf 0.20, fund_value 0.13,
                  fund_quality 0.12, tech_trend 0.15, tech_momentum 0.10,
                  peer 0.10, behavioral 0.05 → sum=1.0)
            → llm_summary (enriched 3-4 sentence summary)
            → Live sector-aware shocks via MCP get_scenario_shocks (QQQ/XLP/XLF per sector)
   → Yields data response
```

**Portfolio Holdings Extraction**: `stream()` uses `extract_holdings(query, exclude_ticker=ticker)` from `shared/ticker_utils.py` to extract holdings from natural language (e.g. "My portfolio holds AAPL, MSFT, GOOGL"). Holdings are passed through the full chain: `stream()` → `analyze()` → `graph.run()` → `correlation_node`.

**Correlation only on explicit request**: The orchestrator prompt instructs the LLM to include holdings in the quant agent task only when the user explicitly mentions portfolio holdings or asks for correlation in their current message. Memory context portfolio lines are labelled as background reference so the LLM does not auto-include them for every single-ticker query.

**Correlation Matrix Notes**: When no holdings are provided, returns `{"note": "No portfolio holdings provided..."}` instead of `{}`. When price data is insufficient or computation fails, returns a descriptive error.

**DCF Fix**: The DCF valuation now correctly reads free cash flow data from the `cash_flow` financial statement (not `income_statement`). This fixes the issue where DCF valuations were returning null.

**DCF Skip Messaging**: When annual volatility exceeds the 35% threshold, `compute_metrics_node` sets a descriptive `dcf_error` field (e.g. "DCF skipped: annual volatility (41.0%) exceeds 35% threshold – routed to stress test instead"). This error is propagated through the graph into the final output and LLM reasoning, providing visibility into why DCF was not computed.

**Sector-Relative Scoring**: `format_output_node` now passes `peer_comparison.medians` (sector medians for PE, EV/EBITDA, ROE, margins, D/E) into `_score_fundamental_value` and `_score_fundamental_quality`. When available, these use `_relative_score()` to score metrics relative to the sector median instead of absolute universal thresholds — making fundamental scoring sector-aware.

**Live Scenario Shocks**: `stress_test_node` fetches `get_scenario_shocks(sector)` via MCP for live historical crash returns per sector ETF (QQQ for tech, XLF for financials, etc.), replacing hardcoded S&P-only fallbacks.

### Quant Agent — LangGraph Fan-In Topology

The quant agent's LangGraph state machine required three fixes in v1.33-1.34 to handle concurrent fan-in writes:

- **Annotated reducers** (`agent_3_langgraph/state.py`): State keys written by multiple nodes (`metrics`, `reasoning`, `recommendation`, `stress_test_result`, `dcf_error`) use `Annotated[type, reducer]` — `_merge_dict`, `_last_str`, `_last_nonnull`. Without reducers, LangGraph raises `INVALID_CONCURRENT_GRAPH_UPDATE` when two nodes write to the same key in the same checkpoint step.

- **Diamond dependency removed** (`agent_3_langgraph/graph.py`): The direct `fetch_fundamentals → format_output` edge was removed. `fetch_fundamentals` already fans into `peer_comparison_node` which fans into `format_output` — the direct edge created a diamond pattern where `format_output` triggered twice in the same step.

- **Passthrough keys removed** (`agent_3_langgraph/nodes.py`): `format_output_node` was returning copies of state keys (`positioning`, `dcf_valuation`, `correlation_matrix`, `fundamentals`) that other nodes already wrote. Now only emits `recommendation`, `reasoning`, `metrics`, and `stress_test_result` — only what it actually computes.

### Market Context Agent (CrewAI)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(MarketContextAgent)
  → MarketContextAgent.stream()
    → 3-step parallel data collection (Phase 3):
      ├── Step 1: get_macro_indicators() ∥ get_financials(ticker)
      │     → macro regime (yields, VIX, DXY, sector ETFs, yield curve)
      │     → primary financials (sector/industry for peer resolution)
      ├── Step 2: resolve peers via MCP get_peers (dynamic yfinance Industry/Sector)
      └── Step 3: asyncio.gather(peer financials, peer prices)
    → 1-agent CrewAI ("Market Context Analyst"):
      → Outputs JSON: narrative, macro_regime, relative_peer_positioning,
        overall_signal, confidence_score (0-1), key_tailwinds, key_headwinds
  → Yields data response
```

**Note**: The old Sentiment agent fetched `get_news_sentiment` and `get_company_filings`. Both were redundant with the RAG agent after Phase 1 and are no longer called. Market Context exclusively owns macro regime analysis and peer landscape positioning.

## Caching Layer

Four independent caching tiers reduce latency and external API load:

### MCP Tool-Result Cache (Tier 1A)

`TTLCache` (or `RedisCache` when `REDIS_URL` is configured) in `mcp_servers/finsight_server.py` — created via `make_cache()` factory in `shared/redis_cache.py`:

| Cache | TTL | Key | Notes |
|---|---|---|---|
| `_cache_prices` | 1 min | `(ticker, period, interval)` | yfinance OHLCV |
| `_cache_benchmark` | 1 h | ticker | Index benchmarks (^GSPC, ^VIX, etc.) |
| `_cache_financials` | 1 h | `(ticker,)` | income/balance/cashflow |
| `_cache_news` | 5 min | `(ticker, limit)` | only cached when articles found |
| `_cache_macro` | 15 min | `"macro"` | Treasury yields, VIX, DXY, sector ETFs |
| `_cache_filing` | permanent (LRU-200) | `edgar_url` | filings are immutable |
| `_cache_submissions` | 6 h | `cik` | EDGAR CIK submissions |
| `_cache_benchmark` | 1 h | ticker | `^GSPC` and other index benchmarks |
| `_cache_peers` | 24 h | ticker | yfinance Industry/Sector peer lists |
| `_cache_shocks` | 7 days | sector | Historical crash returns per sector ETF |
| `_cache_peers` | 24 h | ticker | Yahoo Finance Industry/Sector peer lists |
| `_cache_shocks` | 7 days | sector | Historical crash returns per sector ETF |

### Redis Two-Level Cache (Tier 1C, v1.31)

`shared/redis_cache.py` — L1 (in-process `TTLCache`) + L2 (Redis write-through). Created via `make_cache()`:

```python
def make_cache(ttl_seconds=300, name=""):
    if REDIS_URL:
        return RedisCache(ttl_seconds=ttl_seconds, name=name)
    return TTLCache(ttl_seconds=ttl_seconds)
```

L1 miss → read from Redis → populate L1. Every L1 `set()` propagates to Redis via write-through. Transparent drop-in: when `REDIS_URL` is unset, `make_cache()` returns a bare `TTLCache` with identical behavior.

### LangChain SQLiteCache (Tier 1B)

`agent_3_langgraph/nodes.py` sets `SQLiteCache(database_path="db/.langchain_cache.db")` before the `ChatOpenAI` instance is used in `llm_summary_node`. Identical ticker+metrics inputs reuse cached LLM output without an LM Studio round-trip.

### Semantic Cache (Tier 1D)

`shared/semantic_cache.py` — ChromaDB collection `finsight_semantic_cache` + `all-MiniLM-L6-v2` embedder (already in-use). Cosine similarity threshold: 0.95; response stored up to 4000 chars in Chroma metadata.

**Date-scoped** (v1.32): `SemanticCache.set()` tags entries with `YYYY-MM-DD` in Chroma metadata. `SemanticCache.get()` uses a `where` filter on `date` — same query on a different day misses cache. Prevents stale cross-day results without a TTL.

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
│  ├── resource://agent_cards/list   │  ├── get_financials()            │
│  └── resource://agent_cards/{name}  │  ├── get_options_chain()         │
│                         │  ├── get_company_filings()       │
│                         │  ├── get_financial_filings()     │
│                         │  ├── get_filing_content()        │
│                         │  ├── validate_ticker()           │
│                         │  ├── resolve_company_ticker()    │
│                         │  ├── full_text_search()          │
│                         │  ├── get_news_sentiment()        │
│                         │  ├── get_earnings_calendar()         │
│                         │  ├── get_insider_transactions()    │
│                         │  ├── get_peers()                   │
│                         │  ├── get_scenario_shocks()         │
│                         │  └── execute_python()            │
└──────────────────────────────────────────────────────┘
```

Agent cards loaded from `agent_cards/*.json`, embedded via `sentence-transformers`, queried via `find_agent` tool using dot-product similarity.

## Timeout Architecture

Timeouts configured via `.env` with `A2A_TIMEOUT=680.0`:

| Layer | Timeout | Mechanism |
|---|---|---|
| A2A discovery | 10s per URL | httpx.AsyncClient within A2ACardResolver |
| A2A messaging (global) | 680s | ClientConfig + httpx.AsyncClient |
| A2A — RAG agent | 600s | `asyncio.wait_for` in `send_message` |
| A2A — Quant agent | 600s | `asyncio.wait_for` in `send_message` |
| A2A — Market Context agent | 600s | `asyncio.wait_for` in `send_message` |
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

All agents use LM Studio (OpenAI-compatible local API). The `config.py` default is `qwen/qwen3-30b-a3b-2507`; developers commonly override to `mistralai/ministral-3-14b-reasoning` via `.env` for faster local inference.

| Agent | Model (default) | Provider |
|---|---|---|
| Orchestrator (ADK) | `qwen/qwen3-30b-a3b-2507` | `openai/` prefix (LM Studio endpoint) |
| RAG (LlamaIndex) | `qwen/qwen3-30b-a3b-2507` | `llama-index-llms-openai-like` |
| Quant (LangGraph) | `qwen/qwen3-30b-a3b-2507` | `langchain-openai` |
| Market Context (CrewAI) | `qwen/qwen3-30b-a3b-2507` | CrewLLM (OpenAI-compatible) |

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
| Market Context Agent | `market_context_agent` | CrewAIInstrumentor, StarletteInstrumentor | `market-context-agent-stream` span |
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
DatabaseSessionService(db_url="sqlite+aiosqlite:///./db/finsight_memory.db")
```

All conversation events (user messages, agent responses, tool calls) are persisted to SQLite tables (`sessions`, `events`). Conversations survive server restarts.

### Timezone

All datetime operations use **IST (UTC+5:30)**, defined as `IST = timezone(timedelta(hours=5, minutes=30), name="IST")` in `shared/config.py`. This applies to agent timestamps, memory created_at fields, analysis_date comparisons, and performance evaluation timestamps. Previously mixed UTC/local timestamps caused same-day cache mismatches on non-IST machines.

### Memory Cache Callback (before_agent_callback)

The fastest same-day cache path is the `before_agent_callback` registered as `root_agent.before_agent_callback = _memory_cache_callback`. It fires before the LLM runs, extracts the user's ticker from session events, and if today's brief has a valid `response_text`, returns `types.Content(role="model", parts=[...])` — the ADK runner accepts this as the agent response and skips the LLM entirely. This completes in ~200ms vs 30-60s for a full agent run.

**Ticker-resolution fallback**: When the regex-extracted token misses in DB (e.g. user typed "VISA" but the brief is stored under canonical "V"), the callback falls back to MCP `resolve_company_ticker` and retries the cache lookup. Closes the asymmetry where `save_brief` dedup hit but the cache lookup missed.

The executor-level path (`agent_1_adk/agent_executor.py`) has a parallel check via `_get_today_cached_text()` for A2A requests.

### Full Synthesis in save_brief

`save_brief` now reads the longest LLM-generated text from `session.events` on the first write (via `_synthesis_text_from_context`). This means both the ADK-web and A2A paths store the full BUY/HOLD/SELL analysis instead of the short rationale. Only falls back to rationale when no model output exists in the turn. The post-turn `update_response_text` overwrite was removed — it was unreliable and blind to the A2A path. The same-day cache callback reads this rich `response_text` directly, so subsequent same-day queries return the full analysis.

### Memory Context Injection

When the cache callback misses (no today brief), the executor injects memory context into the user message:

```
User Query → Executor._build_memory_context(query)
  ├── extract_ticker(query) → "NVDA"
  ├── TickerMemory.get_latest("NVDA") → last brief (with analysis_date)
  ├── Compare analysis_date with today
  │     [TODAY]  → tag as current; LLM MUST return directly (strict directive)
  │     [STALE]  → tag as outdated; LLM MUST call all agents fresh
  ├── PortfolioStore.get() → current holdings
  └── Prepend: [MEMORY CONTEXT] ... [/MEMORY CONTEXT]
       → Runner receives augmented query
```

The memory context is compact (~300 tokens) and includes:
- Latest recommendation for the queried ticker tagged `[TODAY]` or `[STALE]` based on `analysis_date`
- Current portfolio holdings (labelled as background reference — not forwarded to sub-agents unless user explicitly requests portfolio analysis)
- When serving from today's cache (`[TODAY]`), the response is **not** re-saved to memory to prevent duplicate records

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
| Market Context Agent | `http://localhost:8004/health` | `{"status":"ok","agent":"market_context"}` |
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
| Market Context Agent | `logs/market_context.log` |
| MCP Server | `logs/mcp.log` |
| Memory callback | `logs/memory_callback.log` |

### SQLite Schema

```sql
sessions (id, user_id, created_at, updated_at)
events (id, session_id, event_type, data, created_at)
ticker_briefs (id, ticker, recommendation, confidence, response_text, created_at, analysis_date)
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
| Market Context | `score_market_context_response()` | Faithfulness, macro_regime_analysis, peer_landscape_quality | Faithfulness: verifies narrative is grounded in collected macro and peer data. macro_regime_analysis: evaluates if narrative discusses yield curve, VIX, DXY, sector ETF performance with actual values. peer_landscape_quality: evaluates depth of peer comparison across multiple metrics. | `user_input`, `response`, `_retrieved_contexts` (macro + peer data) |

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

### Database Files

All databases are stored under the `db/` folder at the project root — the entire folder is excluded from git via `.gitignore`.

- `db/finsight_memory.db` — ticker briefs, portfolios, performance records, ingested filings
- `db/adk_sessions.db` — ADK conversation sessions and events (separated from memory data in v1.24 to prevent schema conflicts)
- `db/chroma_db/` — ChromaDB vector store for SEC filing RAG and semantic cache
- `db/.langchain_cache.db` — LangChain SQLiteCache for quant agent LLM responses
- All files auto-created on first run; `db/` directory created by `get_db()` via `path.parent.mkdir(parents=True, exist_ok=True)`
