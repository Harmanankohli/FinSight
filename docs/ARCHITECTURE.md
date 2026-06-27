# Architecture

## Overview

FinSight is a multi-agent investment research system where five specialized agents communicate via the **Google A2A Protocol** (Agent-to-Agent). The orchestrator (ADK `LlmAgent`) discovers sub-agents at startup via `A2ACardResolver`, delegates tasks via a single `send_message` tool, and the LLM routes to all agents in parallel (instructed via system prompt to emit all `send_message` calls in one assistant turn). Each sub-agent processes tasks internally using its own framework and tools.

## Trust Boundaries (Auth)

Phase 2 introduced three trust boundaries between components. All default to open (`AUTH_ENABLED=false`); when enabled, each boundary requires specific credential types:

```
                           AUTH_ENABLED=true
+------------------------------------------------------------------+
│                        Boundary A (User↔Frontend↔Orchestrator)   —
│  Browser --https--> Next proxy --Bearer JWT--> Starlette app      —
│  /login  → refresh cookie  —  /api/* → access_token in header     —
│  Public: /health, /.well-known/, /auth/login|refresh|logout       —
│  AuthMiddleware(accept={"user","service"}) on all /api/* paths    —
+------------------------------------------------------------------+
                         ↓
                         │ A2A JSON-RPC (Boundary B)
                         │ Service bearer token in Authorization header
                         ↓
+------------------------------------------------------------------+
│                     Boundary B (Orchestrator↔Sub-Agents)         —
│  Orchestrator --A2A /a2a--> Sub-agent (RAG/Quant/Market)         —
│  Service token in client default headers                          —
│  AuthMiddleware(accept={"service"}) on /a2a, /release-evals       —
│  User context propagated via A2A message metadata (_user envelope)—
+------------------------------------------------------------------+
                         ↓
                         │ MCP SSE (Boundary C)
                         │ Service bearer token in SSE headers
                         ↓
+------------------------------------------------------------------+
│                     Boundary C (Agents↔MCP Server)               —
│  Sub-agents --SSE--> MCP Server (finsight-mcp)                   —
│  MCPServerConfig.headers injects bearer token                     —
│  AuthMiddleware(accept={"service"}) on SSE Mount                  —
│  Public: /health (compose healthchecks)                           —
+------------------------------------------------------------------+
```

When `AUTH_ENABLED=false` (default): all boundaries are open, `X-FinSight-User-Id` header used as dev convention for user identity (no verification).

## Communication Pattern

```
+--------------------------------------------------------------+
│                    A2A Protocol Layer                         —
│  +------------------------------------------------------+    —
│  — Agent Card Discovery                                  │    —
│  — GET /.well-known/agent-card.json                      │    —
│  — - name, skills (id + description), interfaces         │    —
│  +------------------------------------------------------+    —
│  +------------------------------------------------------+    —
│  — JSON-RPC over HTTP (streaming)                        │    —
│  — POST /a2a  events: task, status_update, artifact_upd  —    —
│  — Headers: A2A-Version: 1.0                             │    —
│  +------------------------------------------------------+    —
│  +------------------------------------------------------+    —
│  — Task Lifecycle                                        │    —
│  — SUBMITTED → WORKING → (artifacts) → COMPLETED        │    —
│  +------------------------------------------------------+    —
+--------------------------------------------------------------+
```

## Orchestrator Architecture

The orchestrator (`src/orchestrator/`) uses a single `LlmAgent` with two tools (`send_message`, `load_memory`). The `save_brief` function is defined but no longer exposed as an LLM tool — briefs are auto-saved via `after_agent_callback`. Two parallel cache paths can short-circuit the LLM entirely on same-day repeat queries:

```
Module load (standalone — src/orchestrator/main.py):
  → SubAgentClient.discover() in lifespan background task
    +-- A2ACardResolver(httpx.AsyncClient, url) per seed URL
    +-- Returns typed AgentCard (protobuf)
    +-- self.agents populated

ADK Web UI (src/orchestrator/services.py fires memory service registration):
  → Discovery fires inside _memory_cache_callback on first turn
    — root_agent.instruction = _instruction_provider (callable, per-turn rebuild)

A2A Request → FinSightAgentExecutor.execute()
  → _get_today_cached_text(ticker)  — [CACHE: return today brief if exists]
  → _build_memory_context(query)    │ inject [MEMORY CONTEXT] with [TODAY]/[STALE] tag
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
    → _release_sub_agent_evals() — POST /release-evals to all sub-agents (they defer evals until this signal)
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

## Agent Server Factory (v1.41)

All five sub-agent servers (`src/financial_rag/server.py`, `src/quant/server.py`, `src/market_context/server.py`, `src/analytics/server.py`, `src/reviewer/server.py`) use `build_agent_app()` from `src/shared/agent_server.py`. This shared factory:
- Creates Starlette app with A2A routes, health check, agent card, and `/release-evals` endpoint
- Wraps with `AuthMiddleware` when auth is enabled
- Sets up Langfuse instrumentation, file logging, and startup warm-up hooks
- Eliminates ~100 lines of duplicate server setup per sub-agent

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
        +-- self._ensure_ingested(ticker) — runs in background, non-blocking
        +-- self._ensure_news_ingested(ticker) — runs in background, non-blocking
      → FinancialIndexManager.query(ticker, query) — returns from indexed data
        +-- _classify_query_intent() → sec_filings → news → earnings
        +-- Multi-collection retrieval with score-sorted dedup
        +-- LlamaIndex response synthesizer (response_mode="compact")
      → No intermediate WORKING yield — follows single-yield pattern (one data response on completion)
  → Yields data response with summary + sources

RAGAgent._ensure_ingested(ticker) [background]:
  → MCPClient.connect_all()
  → MCP: get_company_filings(ticker) → filings with edgar_url + ix_url
  → Filter: is_filing_ingested(edgar_url) — skip already-indexed URLs
  → Parallel fetch via asyncio.gather:
    +-- MCP: get_filing_content(edgar_url, ix_url) for candidate 1
    +-- MCP: get_filing_content(edgar_url, ix_url) for candidate 2
    +-- ... (all candidates concurrently, truncated server-side at 25k chars)
  → DocumentIngestionPipeline.ingest_sec_filings_batch() → ChromaDB
  → mark_filing_ingested(edgar_url, ticker) for each new filing
```

**Incremental Ingestion**: `_ensure_ingested()` checks the `ingested_filings` SQLite table before fetching any filing content. URLs already indexed in a previous run are skipped entirely — restarts and same-day re-queries do not re-ingest immutable historical filings.

**Startup warm-up** (`src/financial_rag/server.py`, v1.32): `_do_prewarm()` runs once on Starlette `on_startup` in a thread executor via `asyncio.to_thread`. Three stages: HuggingFace embedder pre-load + dummy encode, three ChromaDB collections (`sec_filings`/`news`/`earnings`) via `get_or_create_collection`, CrossEncoder reranker from `HybridSearchPipeline`. Each stage logs elapsed seconds. Effect: first RAG query pays ~0s model-load tax (was ~3-5s). Warm-up errors are logged but don't crash the server.

**Content Ingestion**: Fetches actual SEC filing content (10-K, 10-Q, 8-K) via `get_filing_content()`, which extracts text from raw EDGAR URLs with fallback to IXBRL viewer URLs.

### Quant Agent (LangGraph)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(QuantAgent)
  → QuantAgent.stream()
    → extract_holdings(query) → portfolio_holdings list
    → analyze(ticker, portfolio_holdings=holdings)
      [Parallel fan-out from START]
        +-- fetch_prices [MCP: get_prices → parse Close data]
        │     → compute_base_metrics (Sharge, vol, VaR, beta)
        │     → technical_analysis (SMA, MACD, RSI, Bollinger, trend)
          │     → volatility gate
          │         +-- high vol (> 35%) → stress_test [sector-aware scenario shocks]
          │         +-- low vol (= 35%) → dcf_valuation [data-driven WACC + growth]
          │     → monte_carlo (GBM, 5k paths, 252d horizon) — runs in BOTH paths
          +-- fetch_fundamentals [MCP: get_financials → 25+ ratios]
          |     → PE, PB, ROE, margins, D/E, growth, golden cross
          |     → peer_comparison (dynamic yfinance Industry/Sector peers, ranks on PE,
          |         EV/EBITDA, growth, margins, ROE, D/E + sector medians for relative scoring)
          +-- options_flow_node (put/call vol ratio, OI ratio, flow signal, no-data handling)
          +-- insider_signals_node (get_insider_transactions MCP — structured buy/sell data)
          +-- analyst_positioning_node (consensus, upside %, short interest, squeeze)
          [Fan-in]
            +-- portfolio_correlation [MCP: get_prices per holding + target]
            +-- format_output (8-group weighted voting: risk 0.15, dcf 0.20, fund_value 0.13,
                  fund_quality 0.12, tech_trend 0.15, tech_momentum 0.10,
                  peer 0.10, behavioral 0.05 → sum=1.0)
            → llm_summary (enriched 3-4 sentence summary)
            → Live sector-aware shocks via MCP get_scenario_shocks (QQQ/XLP/XLF per sector)
   → Yields data response
```

**Portfolio Holdings Extraction**: `stream()` uses `extract_holdings(query, exclude_ticker=ticker)` from `src/shared/ticker_utils.py` to extract holdings from natural language (e.g. "My portfolio holds AAPL, MSFT, GOOGL"). Holdings are passed through the full chain: `stream()` → `analyze()` → `graph.run()` → `correlation_node`.

**Correlation only on explicit request**: The orchestrator prompt instructs the LLM to include holdings in the quant agent task only when the user explicitly mentions portfolio holdings or asks for correlation in their current message. Memory context portfolio lines are labelled as background reference so the LLM does not auto-include them for every single-ticker query.

**Correlation Matrix Notes**: When no holdings are provided, returns `{"note": "No portfolio holdings provided..."}` instead of `{}`. When price data is insufficient or computation fails, returns a descriptive error.

**DCF Fix**: The DCF valuation now correctly reads free cash flow data from the `cash_flow` financial statement (not `income_statement`). This fixes the issue where DCF valuations were returning null.

**DCF Skip Messaging**: When annual volatility exceeds the 35% threshold, `compute_metrics_node` sets a descriptive `dcf_error` field (e.g. "DCF skipped: annual volatility (41.0%) exceeds 35% threshold — routed to stress test instead"). This error is propagated through the graph into the final output and LLM reasoning, providing visibility into why DCF was not computed.

**Sector-Relative Scoring**: `format_output_node` now passes `peer_comparison.medians` (sector medians for PE, EV/EBITDA, ROE, margins, D/E) into `_score_fundamental_value` and `_score_fundamental_quality`. When available, these use `_relative_score()` to score metrics relative to the sector median instead of absolute universal thresholds — making fundamental scoring sector-aware.

**Live Scenario Shocks**: `stress_test_node` fetches `get_scenario_shocks(sector)` via MCP for live historical crash returns per sector ETF (QQQ for tech, XLF for financials, etc.), replacing hardcoded S&P-only fallbacks.

### Quant Agent — LangGraph Module Split (v1.41)

`src/quant/nodes.py` (1286 lines) was split into `src/quant/nodes/` package:
- `calculations.py` — `_run_monte_carlo()`, `_score_fundamental_*()`, `_weighted_vote()`
- `data_fetch.py` — `fetch_prices_node`, `fetch_fundamentals_node`
- `technical.py` — `technical_analysis_node`, `compute_metrics_node`
- `dcf.py` — `dcf_valuation_node`
- `monte_carlo.py` — stress analysis, scenario shocks
- `portfolio.py` — `correlation_node`, `portfolio_correlation`
- `summary.py` — `format_output_node`, `llm_summary_node`

Each file is < 400 lines. The `__init__.py` re-exports all node functions through a clean public API.

### Quant Agent — LangGraph Fan-In Topology

The quant agent's LangGraph state machine required fixes across multiple versions to handle concurrent fan-in writes:

- **Annotated reducers** (`src/quant/state.py`): State keys written by multiple nodes (`metrics`, `reasoning`, `recommendation`, `stress_test_result`, `dcf_error`) use `Annotated[type, reducer]` — `_merge_dict`, `_last_str`, `_last_nonnull`. Without reducers, LangGraph raises `INVALID_CONCURRENT_GRAPH_UPDATE` when two nodes write to the same key in the same checkpoint step.

- **Diamond dependency removed** (`src/quant/graph.py`): The direct `fetch_fundamentals → format_output` edge was removed. `fetch_fundamentals` already fans into `peer_comparison_node` which fans into `format_output` — the direct edge created a diamond pattern where `format_output` triggered twice in the same step.

- **Passthrough keys removed** (`src/quant/nodes.py`): `format_output_node` was returning copies of state keys (`positioning`, `dcf_valuation`, `correlation_matrix`, `fundamentals`) that other nodes already wrote. Now only emits `recommendation`, `reasoning`, `metrics`, and `stress_test_result` — only what it actually computes.

- **Data-readiness guard on `llm_summary_node`** (`src/quant/nodes/summary.py`, v2.5): The 5-way fan-in at `format_output` causes LangGraph to fire `format_output` (and consequently `llm_summary`) multiple times as predecessors complete in different supersteps — resulting in 4 sequential LLM calls (~78s) instead of 1. `llm_summary_node` now checks that all predecessor branches (`metrics`, `reasoning`, `recommendation`, `fundamentals`) have written their data before firing the LLM call. Skipped invocations return `{}` silently.

### Market Context Agent (CrewAI)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(MarketContextAgent)
  → MarketContextAgent.stream()
    → 3-step parallel data collection (Phase 3):
      +-- Step 1: get_macro_indicators() → get_financials(ticker)
      │     → macro regime (yields, VIX, DXY, sector ETFs, yield curve)
      │     → primary financials (sector/industry for peer resolution)
      +-- Step 2: resolve peers via MCP get_peers (dynamic yfinance Industry/Sector)
      +-- Step 3: asyncio.gather(peer financials, peer prices)
    → 1-agent CrewAI ("Market Context Analyst"):
      → Outputs JSON: narrative, macro_regime, relative_peer_positioning,
        overall_signal, confidence_score (0-1), key_tailwinds, key_headwinds
  → Yields data response
```

**Note**: The old Sentiment agent fetched `get_news_sentiment` and `get_company_filings`. Both were redundant with the RAG agent after Phase 1 and are no longer called. Market Context exclusively owns macro regime analysis and peer landscape positioning.

### Analytics Agent (PydanticAI)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(AnalyticsAgent)
  → AnalyticsAgent.stream()
    → resolve_and_validate_ticker(query) → ticker, company
    → AnalyticsAgent.analyze(ticker, period="1y")
      → AnalyticsPipeline.run(ticker, period, mcp_client)
        [PydanticAI DAG — 4 nodes in sequence]

        1. FetchDataNode (parallel):
             ├── _fetch_prices(mcp, ticker, "1y") → close_data, ohlcv_data
             └── _fetch_fundamentals(mcp, ticker) → fundamentals_data

        2. AnalyzeNode (5 parallel analyses):
             ├── _detect_trends(price_data)
             │     → SMA20/50/200, MACD, momentum (ROC), RSI
             │     → composite scoring → bull/neutral/bear trend
             ├── _run_forecast(price_data)
             │     → Holt-Winters exponential smoothing (30-day horizon)
             │     → confidence bands (linearly widening)
             ├── _compute_statistics(price_data, mcp_client)
             │     → skewness, kurtosis, Jarque-Bera normality test
             │     → distribution class (lepto/platykurtic/normal)
             │     → SPY correlation (beta, R²) via MCP get_prices("SPY")
             ├── _detect_anomalies(price_data, ohlcv_data, fundamentals_data)
             │     → price spikes (|z| > 2.5 on log-returns)
             │     → volume spikes (|z| > 3.0)
             │     → fundamental outliers (PE <5 or >100, D/E >5)
             │     → severity: none/low/medium/high
             │     → [if severity ≥ medium] web_search catalyst context
             └── _generate_charts(ohlcv_data, price_data)
                   → ChartPayload list for frontend rendering

        3. FormatOutputNode:
             → aggregates 5 signal dimensions (-1, 0, +1 each):
               trend direction, forecast change >5%, anomaly severity,
               leptokurtic distribution
             → avg_signal > 0.3 → bullish; < -0.3 → bearish; else neutral
             → confidence = |avg_signal| clamped to [0, 1]

        4. LLMSummaryNode (terminal):
             → PydanticAI Agent + OpenAI-compatible LLM
             → generates 3-4 sentence prose summary
             → priority: CRITICAL (never starved by eval)
             → output: AnalyticsAgentOutput schema

    → score_analytics_deterministic(result) → schema_validation
    → [if EVAL_ENABLED] defer_eval(score_analytics_response)
    → Yields data response
```

**Analytics Agent — PydanticAI DAG Pipeline**: The agent uses `pydantic-graph` (not LangGraph) for a 4-node sequential pipeline. Each node is a `BaseNode[AnalyticsState, AnalyticsDeps]` that mutates a shared `AnalyticsState` dataclass and transitions to the next node via its return type annotation. The `AnalyticsDeps` dataclass carries `ticker`, `period`, and `mcp_client` through the graph.

**Statistical Analysis**: Uses scipy for normality testing (Jarque-Bera), numpy for moving averages and MACD. SPY correlation is fetched via MCP — failure is non-fatal (returns empty correlations). All `MetricValue` results carry `.status`, `.warning`, `.methodology` metadata.

**Anomaly Catalyst Search**: When anomalies reach medium/high severity, the agent performs a DuckDuckGo web search for "{ticker} stock price spike catalyst news" and filters boilerplate results to provide context for the LLM summary.

**Output Schema** (`AnalyticsAgentOutput`): `ticker`, `trend_analysis` (TrendAnalysis), `forecast` (ForecastResult), `charts` (list[ChartPayload]), `statistical_summary` (StatisticalSummary), `anomalies` (AnomalyReport), `analytics_signal` (bullish/bearish/neutral), `analytics_confidence` (float [0,1]), `reasoning` (LLM prose).

### Reviewer Agent (OpenAI Agents SDK)

```
[Called by orchestrator AFTER all Phase-1 agents complete]

Orchestrator builds SendMessageInput("reviewer", ticker, session_id)
  → sends condensed per-agent summaries + metrics to Reviewer

A2A Request → DefaultRequestHandler → GenericAgentExecutor(ReviewerAgent)
  → ReviewerAgent.stream()
    → extract_trace_context(query) → clean_query
    → Parse JSON payload: {ticker, session_id, agent_outputs}
         ├── Inline agent_outputs (preferred — injected by orchestrator)
         └── Fallback: get_agent_outputs(session_id) from SQLite store
    → [guardrail] payload_structure_guardrail:
         → tripwire if: invalid JSON, missing ticker, missing session_id+agent_outputs

    → Pre-reviewer integrity gate:
         → validate_metric_integrity(agent_outputs)
           → checks: DCF upside % consistency, Sharpe/VaR range, PE/ROE plausibility
           → returns alerts: critical / warning / info severity

    → 6 deterministic tools (no LLM round-trips — pure Python):
         ├── check_contradictions(agent_outputs) → list[ContradictionFlag]
         │     → cross-agent signal conflicts:
         │       quant BUY vs bearish analytics trend (HIGH)
         │       quant BUY vs negative RAG sentiment (MEDIUM)
         │       market bearish vs quant bullish signal (MEDIUM)
         │       DCF vs Monte Carlo divergence >40% (MEDIUM)
         │       quant BUY but RSI >75 overbought (MEDIUM)
         │       quant BUY but Monte Carlo prob_profit <50% (MEDIUM)
         │       anomaly severity vs high confidence (LOW)
         │     → deduplicated by (field, description) pair
         ├── verify_sources(agent_outputs) → list[SourceVerification]
         │     → DCF upside % recalculated vs reported (tolerance 1%)
         │     → RAG summary present but no sources listed
         │     → market signal enum validation (bullish/bearish/neutral)
         │     → analytics forecast dates not in past, chart datasets non-empty
         ├── score_confidence(agent_outputs) → ConfidenceBreakdown
         │     → per-agent confidence derivation:
         │       quant: 30% data freshness + 25% signal agreement + 25% source quality + 20% base
         │       rag: 0.3 base + 0.2 length + 0.3 sources (max 1.0)
         │       market: 0.3 base + 0.2 narrative + 0.1 signal + 0.1 tailwinds + 0.1 headwinds + 0.1 macro
         │       analytics: 0.3 base + 0.2 trend + 0.2 forecast + 0.1 stats + 0.1 charts
         │     → agreement_score: max(bullish,bearish,neutral) / total directional signals
         │     → data_quality: 35% completeness + 35% consistency + 20% freshness + 10% verification
         │     → meta_confidence: 35% avg_agent + 25% agreement + 25% consistency + 15% verification
         ├── validate_recommendation(agent_outputs) → RecommendationValidation
         │     → evaluates quant recommendation against DCF, technicals, macro, fundamentals, MC, RAG
         │     → returns: supporting_evidence[], contradicting_evidence[], evidence_supports, evidence_strength
         ├── check_consistency(agent_outputs) → ConsistencyResult
         │     → RSI extreme warnings (<30 or >70)
         │     → DCF vs Monte Carlo divergence check
         │     → returns: consistency_score (0-1), warnings[], contradiction_summary
         └── validate_dcf(quant) → DCFValidation
               → intrinsic/market ratio sanity: <0.30 (warning: undervalued), >3.0 (warning: overvalued)
               → negative WACC check, zero growth sanity, upside % sign consistency

    → Build synthesis prompt (JSON):
         {ticker, agent_summaries (condensed per-agent),
          contradictions, verifications, confidence (as percentages),
          validation, consistency, dcf_validation, integrity_alerts}

    → Runner.run(reviewer_agent, input=synthesis_prompt)
         → OpenAI Agents SDK agent with Langfuse-instrumented LLM
         → priority: CRITICAL
         → output_type: ReviewerAgentOutput (structured)
         → generates: review_summary, contradictions, source_verifications,
           confidence_breakdown, recommendation_validation, verdict, review_confidence, flags

    → Attach _tool_results for extraction pipeline
    → score_reviewer_deterministic(output) → schema_validation
    → [if EVAL_ENABLED] defer_eval(score_reviewer_response)
    → Yields data response
```

**Reviewer Agent — Design Principles**:

- **Deterministic tools first, LLM once**: All 6 validation tools run in pure Python (no LLM calls). The LLM receives pre-computed results and synthesizes them into a structured report. This keeps the reviewer to exactly 1 LLM call per query.
- **Guardrail on input**: An `InputGuardrail` trips on invalid JSON, missing ticker, or missing agent outputs — prevents the LLM from hallucinating review data on bad input.
- **Pre-reviewer integrity gate**: `validate_metric_integrity()` runs before the LLM and flags critical mathematical impossibilities (e.g., DCF upside that doesn't match intrinsic/current ratio). Critical alerts are attached to the output for downstream visibility.
- **Meta-confidence scoring**: The confidence breakdown distinguishes individual agent confidence from the overall meta-confidence (weighted aggregate). The LLM is explicitly instructed never to conflate them.
- **Structured output**: `ReviewerAgentOutput` enforces `verdict` (BUY/HOLD/SELL), `review_confidence` (meta_confidence), `contradictions`, `source_verifications`, `confidence_breakdown`, `recommendation_validation`, and `flags`.

**Output Schema** (`ReviewerAgentOutput`): `review_summary` (3-5 sentences citing specific numbers), `contradictions` (list[ContradictionFlag]), `source_verifications` (list[SourceVerification]), `confidence_breakdown` (ConfidenceBreakdown with agent_scores, agreement_score, data_quality_score, meta_confidence), `recommendation_validation` (RecommendationValidation), `verdict` (BUY/HOLD/SELL), `review_confidence` (float [0,1]), `flags` (list[str] for data quality concerns).

## Caching Layer

Four independent caching tiers reduce latency and external API load:

### MCP Tool-Result Cache (Tier 1A)

`TTLCache` (or `RedisCache` when `REDIS_URL` is configured) in `src/mcp_tools/finsight_server.py` — created via `make_cache()` factory in `src/shared/redis_cache.py`:

| Cache | TTL | Key | Notes |
|---|---|---|---|
| `_cache_prices` | 1 min | `(ticker, period, interval)` | yfinance OHLCV |
| `_cache_benchmark` | 1 h | ticker | Index benchmarks (^GSPC, ^VIX, etc.) |
| `_cache_financials` | 1 h | `(ticker,)` | income/balance/cashflow |
| `_cache_news` | 5 min | `(ticker, limit)` | only cached when articles found |
| `_cache_macro` | 15 min | `"macro"` | Treasury yields, VIX, DXY, sector ETFs |
| `_cache_filing` | permanent (LRU-200) | `edgar_url` | filings are immutable |
| `_cache_submissions` | 6 h | `cik` | EDGAR CIK submissions |
| `_cache_peers` | 24 h | ticker | yfinance Industry/Sector peer lists |
| `_cache_shocks` | 7 days | sector | Historical crash returns per sector ETF |
| `_cache_analyst_activity` | 1 h | `(ticker,)` | Analyst grading history via yahooquery |
| `_cache_valuation_ts` | 24 h | `(ticker,)` | Quarterly valuation multiples via yahooquery |
| `_cache_earnings_trend` | 1 h | `(ticker,)` | Forward EPS estimates & revisions via yahooquery |

### Redis Two-Level Cache (Tier 1C, v1.31)

`src/shared/redis_cache.py` — L1 (in-process `TTLCache`) + L2 (Redis write-through). Created via `make_cache()`:

```python
def make_cache(ttl_seconds=300, name=""):
    if REDIS_URL:
        return RedisCache(ttl_seconds=ttl_seconds, name=name)
    return TTLCache(ttl_seconds=ttl_seconds)
```

L1 miss → read from Redis → populate L1. Every L1 `set()` propagates to Redis via write-through. Transparent drop-in: when `REDIS_URL` is unset, `make_cache()` returns a bare `TTLCache` with identical behavior.

### LangChain SQLiteCache (Tier 1B)

`src/quant/nodes.py` sets `SQLiteCache(database_path="db/.langchain_cache.db")` before the `ChatOpenAI` instance is used in `llm_summary_node`. Identical ticker+metrics inputs reuse cached LLM output without an LM Studio round-trip.

### Semantic Cache (Tier 1D)

`src/shared/semantic_cache.py` — ChromaDB collection `finsight_semantic_cache` + `all-MiniLM-L6-v2` embedder (already in-use). Cosine similarity threshold: 0.95; response stored up to 4000 chars in Chroma metadata.

**Date-scoped** (v1.32): `SemanticCache.set()` tags entries with `YYYY-MM-DD` in Chroma metadata. `SemanticCache.get()` uses a `where` filter on `date` — same query on a different day misses cache. Prevents stale cross-day results without a TTL.

Wired into `src/orchestrator/agent_executor.py`:
- **Before** `runner.run_async`: `SemanticCache.get(query)` — on hit, return immediately
- **After** successful response: `SemanticCache.set(query, text)`
- Controlled by `SEMANTIC_CACHE_ENABLED=true` env var (off by default)

### KV Cache Prefix (Tier 1C)

`src/orchestrator/agent.py` splits `_build_instruction()` into a module-level `_STATIC_PREAMBLE` constant and a dynamic tail (today's date + agent list). LM Studio reuses the KV-cached static prefix across requests. Same pattern applied to CrewAI backstory strings in `src/market_context/crew.py`.

### LLM Priority Queue (Tier 1E)

`src/shared/llm_queue.py` provides a process-local async priority semaphore (`LLMPriorityQueue`) that throttles LLM calls when the single LM Studio instance is saturated. Three priority tiers:

| Priority | Usage | Behavior |
|---|---|---|
| `CRITICAL` (0) | Quant `llm_summary_node`, CrewAI `crew.kickoff()` | Never starved — production inference served before eval |
| `NORMAL` (1) | Quant server pre-warmup ping | Served after CRITICAL, before LOW |
| `LOW` (2) | RAGAS eval `metric.ascore()` | Yields when production is queued — waits if all slots occupied |

Sized by `LLM_MAX_CONCURRENT` (default 2). Uses `heapq` with `(priority, seq, asyncio.Future)` for O(log n) scheduling. Slots are handed directly to the next waiter on release, preserving priority ordering without race windows. Singleton instance imported where needed.

## Guardrails

### Input Guardrails

`src/orchestrator/agent_executor.py`, top of `execute()`:

1. **Off-topic filter** — `_NON_INVESTMENT_RE` regex matches weather/recipes/entertainment/horoscopes. Returns canned message in < 100 ms, no sub-agents invoked.
2. **Ticker pre-check** — when a ticker is extracted, calls MCP `validate_ticker` before spawning sub-agents. Returns clean error in < 2 s if ticker is invalid.
3. **Semantic cache check** — if `SEMANTIC_CACHE_ENABLED`, checks cache before the orchestrator runs.

### Output Guardrails

`src/orchestrator/agent_executor.py`, in `_process_response()`:

1. **Empty/short response guard** — `len(text.strip()) < 50` → `TASK_STATE_FAILED` with structured error.
2. **Signal check** — if response lacks BUY/HOLD/SELL and the query was a stock analysis request, emits a Langfuse warning span with `missing_signal: true`.

## MCP Architecture

MCP server split from a single monolithic file (`src/mcp_tools/finsight_server.py`, 2095 lines) into a package with per-tool modules (v1.41):

```
mcp_tools/
  +-- _app.py              # 78-line composition root with get_app() factory
  +-- finsight_server.py   # re-exports get_app() for backward compat
  +-- tools/
  │   +-- agent_registry.py   # find_agent(), resource endpoints
  │   +-- market_data.py      # get_prices(), get_financials(), get_options_chain(),
  │                           #   get_valuation_timeseries() (yahooquery),
  │                           #   get_macro_indicators() (yahooquery batch fetch, fallback to yfinance)
  │   +-- edgar.py            # SEC EDGAR tools (filings, content, search)
  │   +-- ticker.py           # validate_ticker(), resolve_company_ticker()
  │   +-- sentiment.py        # news, sentiment indicators, earnings history (with forward
  │                           #   estimates via yahooquery earnings_trend),
  │                           #   get_analyst_activity() (yahooquery grading_history)
  │   +-- sandbox.py          # execute_python() with AST gate
  +-- infra/
      +-- rate_limiters.py    # TokenBucket rate limiter (_YF_LIMITER, _YQ_LIMITER for yahooquery)
      +-- embed.py            # sentence-transformers lazy loader
      +-- news_fetch.py       # RSS feed fetchers
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
| A2A — Analytics agent | 600s | `asyncio.wait_for` in `send_message` |
| A2A — Reviewer agent | 300s | `asyncio.wait_for` in `send_message` |
| MCP tool calls | 30s | MCPClient default |

## Error Handling

| Failure Mode | Strategy |
|---|---|
| Agent discovery failure | Retries 3x with 5s delay |
| A2A timeout | Caught, returns error JSON to LLM |
| MCP connection failure | Exponential backoff (2^attempt), max 3 retries |
| Agent unavailable | Skipped, LLM works with what it has |
| Response parse failure | `json.JSONDecodeError` caught, text used as-is |

## Shared Agent Output Store (v2.7)

`src/shared/memory/agent_output_store.py` provides a cross-process SQLite store for full agent output persistence. The orchestrator's `send_message` callback calls `await store_agent_output(session_id, agent_name, output)` before returning, ensuring the reviewer always has data. The reviewer fetches outputs via `get_agent_outputs(session_id)` which normalizes agent names through `_AGENT_KEY_MAP`.

| Function | Purpose |
|---|---|
| `store_agent_output(session_id, agent_name, output)` | INSERT OR REPLACE into `agent_output_store` table. Called by orchestrator after each sub-agent response. |
| `get_agent_outputs(session_id)` | Fetch all outputs for a session, keyed by normalized short name (rag, quant, etc.). Called by reviewer executor. |
| `prune_stale_outputs(max_age_seconds=600)` | DELETE entries older than TTL. Called at orchestrator startup. |

The `agent_output_store` table (`session_id TEXT, agent_name TEXT, output_json TEXT, created_at TIMESTAMP, PRIMARY KEY (session_id, agent_name)`) was added in schema migration v5→v6.

## Centralized Metrics (MetricValue)

`src/shared/metrics.py` (v2.9) provides a validated `MetricValue` class — a `float` subclass that carries `.status`, `.warning`, `.methodology`, and `.to_dict()` metadata. Used by both Quant and Analytics agents for all metric computations. Replaces 5 near-duplicate metric-formatting code paths.

| Function | Purpose |
|---|---|
| `MetricValue(value, methodology, min_valid, max_valid)` | Float subclass with auto-validation (finite check, range check) |
| `metric_result(value, methodology, ...)` | Factory wrapping raw float into validated MetricValue |
| `compute_rsi_wilder(prices, period)` | RSI via Wilder's smoothed EMA (returns float) |
| `compute_sharpe_ratio(returns, risk_free_rate, periods)` | Annualised Sharpe ratio (returns float) |
| `compute_beta(asset_returns, benchmark_returns)` | CAPM beta (returns float) |
| `compute_sortino_ratio(returns, ...)` | Downside-volatility Sharpe variant (returns float) |
| `compute_calmar_ratio(cagr, max_drawdown)` | CAGR / max drawdown (returns float) |
| `compute_alpha(returns, benchmark, ...)` | Jensen's alpha (returns float) |
| `compute_information_ratio(returns, benchmark, ...)` | Active return / tracking error (returns float) |
| `compute_cagr(values)` | Compound annual growth rate (returns float\|None) |

## Per-Agent Dependency Groups (v2.16)

`pyproject.toml` defines per-agent optional dependency groups so each Docker image installs only its own framework:

| Group | Framework | Used By |
|---|---|---|
| `orchestrator` | google-adk, a2a-sdk, starlette | Orchestrator |
| `rag` | llama-index, sentence-transformers | RAG Agent |
| `quant` | langchain, langchain-openai | Quant Agent |
| `market` | crewai | Market Context Agent |
| `analytics` | pydantic-ai | Analytics Agent |
| `reviewer` | openai-agents | Reviewer Agent |
| `mcp_server` | fastmcp | MCP Server |

Core `[project.dependencies]` contains only the shared base (~30 packages: a2a-sdk, starlette, pydantic, observability, auth). Each Dockerfile installs `".[svc]"` via `uv pip install --system`. Package counts per image dropped from 315 to 99-184.

## CI Pipeline

A GitHub Actions CI pipeline runs on every push (v1.41, Phase 0):

| Job | Command | Scope |
|---|---|---|
| **lint** | `ruff check .` + `ruff format --check` | All Python files |
| **type** | `mypy shared orchestrator` | shared/ + orchestrator only (other modules escape-hatched) |
| **test** | `pytest tests/unit/ -v` | Unit tests with slim deps (no PyTorch/CUDA) |
| **frontend** | `npx next lint` + `npx tsc --noEmit` | web/nextjs-app/ |
| **openapi** | `python scripts/generate_openapi.py --check` | docs/openapi.json freshness |
| **docker** | Docker build for all 8 services (continue-on-error) | 7 Python Dockerfiles + 1 Next.js Dockerfile |

Test job uses `--no-deps -e .` to install only ~15 packages instead of all 293 base deps (avoids PyTorch, CUDA, all agent frameworks). A `AUTH_ENABLED={false,true}` matrix runs auth contract tests twice. The Docker build matrix includes `web-frontend` (Next.js standalone image) alongside the 7 Python agent images.

## Centralized Settings & Bootstrap

Configuration migrated from `src/shared/config.py` (re-exporting shim, removed in v2.0) to `src/shared/settings.py`:

- **pydantic-settings `BaseSettings`**: Type-safe env var loading with back-compat aliases (`LLM_BASE_URL` → `OPENAI_BASE_URL` → `LM_STUDIO_BASE_URL`)
- **`get_settings()` singleton**: Lazy-loaded, cached after first call
- **`validate_runtime()`**: Production-mode enforcement (e.g., refuses `ast` sandbox on Windows)

Process-level side-effects centralized in `src/shared/bootstrap.py`:
- Event loop policy (`WindowsSelectorEventLoopPolicy` on Windows)
- `HF_HUB_OFFLINE=1`
- `sys.stdout.reconfigure(encoding='utf-8')`
- `LANGFUSE_SECONDARY_KEY` patching

## LLM Configuration

All agents use LM Studio (OpenAI-compatible local API). The `src/shared/settings.py` default is `mistralai/ministral-3-14b-reasoning`; the active `.env` overrides to `liquid/lfm2.5-1.2b` for sub-agents and `openai/qwen/qwen3-4b-2507` for the ADK orchestrator. All LLM calls are throttled by `LLMPriorityQueue` (3 priority tiers) to prevent eval scoring from starving production inference; concurrency controlled by `LLM_MAX_CONCURRENT` env var (default 2).

### Cross-Process Eval Coordination

Sub-agent evals are deferred via `src/shared/eval_gate.py` to prevent five sub-agent eval processes from competing with orchestrator synthesis on the shared LM Studio instance. Each sub-agent calls `defer_eval()` instead of `asyncio.create_task()`. The orchestrator's `after_agent_callback` fires `_release_sub_agent_evals()` which POSTs to each sub-agent's `/release-evals` endpoint after synthesis completes. A 120s safety-net auto-release prevents evals from being silently dropped if the orchestrator crashes.

| Agent | Model (default / .env override) | Provider |
|---|---|---|
| Orchestrator (ADK) | `mistralai/ministral-3-14b-reasoning` (default) / `openai/qwen/qwen3-4b-2507` (.env) | `openai/` prefix (LM Studio endpoint) |
| RAG (LlamaIndex) | `mistralai/ministral-3-14b-reasoning` (default) / `liquid/lfm2.5-1.2b` (.env) | `llama-index-llms-openai-like` |
| Quant (LangGraph) | `mistralai/ministral-3-14b-reasoning` (default) / `liquid/lfm2.5-1.2b` (.env) | `langchain-openai` |
| Market Context (CrewAI) | `mistralai/ministral-3-14b-reasoning` (default) / `liquid/lfm2.5-1.2b` (.env) | CrewLLM (OpenAI-compatible) |

## Observability & Tracing

Langfuse provides distributed tracing across all six agent processes. Trace context is propagated through the A2A protocol via text-based injection.

### Trace Propagation Flow

```
Orchestrator (orchestrator)
  +-- agent_executor.py: langfuse.trace(name="finsight-query")
  +-- start_as_current_observation(name="orchestrator-execute")
  +-- send_message() tool: get_current_trace_id() + get_current_observation_id()
  +-- inject_trace_context(task, trace_id, parent_span_id) → A2A text prefix

A2A Protocol (JSON-RPC over HTTP)
  +-- Task text prefixed with {"_trace": {"trace_id": "...", "parent_span_id": "..."}}

Sub-agents (agent_2, agent_3, agent_4)
  +-- extract_trace_ids(query) → (trace_id, parent_span_id, clean_query)
  +-- start_observation(trace_context={"trace_id": ..., "parent_span_id": ...})
  +-- Langfuse joins the span to the orchestrator's trace tree
```

### Trace Context Utility

| Function | File | Purpose |
|---|---|---|
| `inject_trace_context(task, trace_id, parent_span_id)` | `src/shared/trace_context.py` | Serializes trace IDs as JSON prefix in task text |
| `extract_trace_context(task)` | `src/shared/trace_context.py` | Returns `(trace_ctx_dict, clean_query)` |
| `extract_trace_ids(task)` | `src/shared/trace_context.py` | Returns `(trace_id, parent_span_id, clean_query)` |

### Span Noise Filtering

The Langfuse client uses `is_default_export_span` to filter out noisy A2A internal spans (`a2a-python-sdk` scope) and HTTPX transport spans. Only high-level workflow spans (`langfuse-sdk` scope) and LLM spans are exported, keeping traces clean and focused.

### Per-Agent Instrumentation

| Agent | Service Name | Instrumentors | Manual Spans |
|---|---|---|---|
| Orchestrator | `orchestrator` | GoogleADKInstrumentor, HTTPXClientInstrumentor | `orchestrator-execute` span, per-sub-agent latency spans |
| RAG Agent | `rag_agent` | LlamaIndexInstrumentor | `rag-agent-stream` span |
| Quant Agent | `quant_agent` | StarletteInstrumentor | `quant-agent-stream` span + CallbackHandler for LangGraph nodes |
| Market Context Agent | `market_context_agent` | CrewAIInstrumentor, StarletteInstrumentor, `@observe()` on `analyze()` | `market-context-agent-stream` span + `crewai-market-analysis` observation |
| Analytics Agent | `analytics_agent` | PydanticAI `Agent.instrument_all()`, StarletteInstrumentor | `analytics-agent-stream` span + `gen_ai.*` OTEL spans |
| Reviewer Agent | `reviewer_agent` | StarletteInstrumentor, `langfuse.openai.AsyncOpenAI` | `reviewer-agent-stream` span + auto-instrumented LLM generations |
| MCP Server | `mcp_server` | — | `@observe()` on individual tools |

### Colored Console Logging (v2.17)

`src/shared/logging_config.py` provides a `ColoredFormatter` for console output:
- Per-level ANSI colors (DEBUG=cyan, INFO=green, WARNING=yellow, ERROR=red, CRITICAL=bold white on red)
- Service badge colors aligned with the frontend CSS palette
- Decorator lifecycle markers: `→ Enter`, `← Exit ⏱`, `✗ Fail ⏱`
- `NO_COLOR=1` disables all ANSI codes; `FORCE_COLOR=1` forces them on non-TTY
- JSON file logs remain plain (zero ANSI codes)
- Frontend counterpart: `src/web/nextjs-app/lib/logger.ts` with ANSI (server) and CSS `%c` (browser)

### Sub-Agent Latency Spans

`src/orchestrator/sub_agent_client.py` wraps each `send_message()` call with a `time.monotonic()` stopwatch and emits a Langfuse span:

```python
lf.observation(
    as_type="span",
    name=f"sub-agent-{agent_name}",
    input={"task": task_str[:200]},
    output={"response": result_text[:200]},
    metadata={"latency_ms": round((t1 - t0) * 1000), "agent": agent_name},
)
```

When `EVAL_TRACE_ENABLED=true`, the same call also writes a JSON file to `src/tests/evaluation/eval_results/orchestrator_traces/` for use by the RAGAS orchestrator evaluation runner.

### TTFT Tracking (v2.18)

Time to First Token (TTFT) is tracked in two locations via Langfuse `generation.update(completion_start_time)`:

1. **`sub_agent_client.py`**: Each A2A `send_message()` call wraps the streaming response. On the first event received, `generation.update(completion_start_time=now)` marks when the sub-agent started producing output. This gives per-sub-agent TTFT in the Langfuse trace tree.

2. **`agent_executor.py`**: The ADK runner's `run_async()` stream similarly records the first event as `completion_start_time`, measuring the orchestrator LLM's TTFT.

Both locations use a local `ttft_recorded` flag to ensure `completion_start_time` is set only once per generation. Langfuse computes the TTFT as `completion_start_time - start_time` automatically.

## Memory Layer Architecture

The memory layer provides persistent session storage and cross-session memory retrieval using SQLite. All memory components are in `src/shared/memory/`.

### Overview

```
+--------------------------------------------------------------+
│                    Memory Layer (SQLite)                       —
│                                                               —
│  +-----------------+  +-----------------+  +---------------+ —
│  — Session Store   │  — Ticker Memory   │  — Portfolio     │ —
│  — (ADK native)    │  — (briefs/recs)   │  — Store         │ —
│  — DatabaseSession —  — format_context()—  — holdings/risk — —
│  +-----------------+  +-----------------+  +---------------+ —
│                                                               —
│  +-----------------+  +-----------------+                    —
│  — Performance     │  — Memory Service  —                    —
│  — Tracker         │  — (load_memory)   │                    —
│  — accuracy stats  —  — cross-session   │                    —
│  +-----------------+  +-----------------+                    —
+--------------------------------------------------------------+
```

### Session Persistence

`DatabaseSessionService` (ADK native) replaces `InMemorySessionService`:

```python
DatabaseSessionService(db_url="sqlite+aiosqlite:///./db/finsight_memory.db")
```

All conversation events (user messages, agent responses, tool calls) are persisted to SQLite tables (`sessions`, `events`). Conversations survive server restarts.

### Timezone

All datetime operations use **IST (UTC+5:30)**, defined as `IST = timezone(timedelta(hours=5, minutes=30), name="IST")` in `src/shared/settings.py`. This applies to agent timestamps, memory created_at fields, analysis_date comparisons, and performance evaluation timestamps. Previously mixed UTC/local timestamps caused same-day cache mismatches on non-IST machines.

### Memory Cache Callback (before_agent_callback)

The fastest same-day cache path is the `before_agent_callback` registered as `root_agent.before_agent_callback = _memory_cache_callback`. It fires before the LLM runs, extracts the user's ticker from session events, and if today's brief has a valid `response_text`, returns `types.Content(role="model", parts=[...])` — the ADK runner accepts this as the agent response and skips the LLM entirely. This completes in ~200ms vs 30-60s for a full agent run.

**Ticker-resolution fallback**: When the regex-extracted token misses in DB (e.g. user typed "VISA" but the brief is stored under canonical "V"), the callback falls back to MCP `resolve_company_ticker` and retries the cache lookup. Closes the asymmetry where `save_brief` dedup hit but the cache lookup missed.

The executor-level path (`src/orchestrator/agent_executor.py`) has a parallel check via `_get_today_cached_text()` for A2A requests.

### Full Synthesis in save_brief

`save_brief` now reads the longest LLM-generated text from `session.events` on the first write (via `_synthesis_text_from_context`). This means both the ADK-web and A2A paths store the full BUY/HOLD/SELL analysis instead of the short rationale. Only falls back to rationale when no model output exists in the turn. The post-turn `update_response_text` overwrite was removed — it was unreliable and blind to the A2A path. The same-day cache callback reads this rich `response_text` directly, so subsequent same-day queries return the full analysis.

### Memory Context Injection

When the cache callback misses (no today brief), the executor injects memory context into the user message:

```
User Query → Executor._build_memory_context(query)
  +-- extract_ticker(query) → "NVDA"
  +-- TickerMemory.get_latest("NVDA") → last brief (with analysis_date)
  +-- Compare analysis_date with today
  │     [TODAY]  → tag as current; LLM MUST return directly (strict directive)
  │     [STALE]  → tag as outdated; LLM MUST call all agents fresh
  +-- PortfolioStore.get() → current holdings
  +-- Prepend: [MEMORY CONTEXT] ... [/MEMORY CONTEXT]
       → Runner receives augmented query
```

The memory context is compact (~300 tokens) and includes:
- Latest recommendation for the queried ticker tagged `[TODAY]` or `[STALE]` based on `analysis_date`
- Current portfolio holdings (labelled as background reference — not forwarded to sub-agents unless user explicitly requests portfolio analysis)
- When serving from today's cache (`[TODAY]`), the response is **not** re-saved to memory to prevent duplicate records

### Component Architecture

| Component | File | Purpose |
|---|---|---|
| `SQLiteStore` | `src/shared/memory/store.py` | SQLite connection, auto-migration, table creation |
| `TickerMemory` | `src/shared/memory/ticker_memory.py` | Per-ticker brief storage, `format_context()` for prompt injection |
| `PortfolioStore` | `src/shared/memory/portfolio_store.py` | User profile, holdings persistence, risk profile |
| `PerformanceTracker` | `src/shared/memory/performance_tracker.py` | Recommendation outcome tracking, accuracy evaluation |
| `SQLiteMemoryService` | `src/shared/memory/memory_service.py` | ADK `BaseMemoryService` implementation for `load_memory` tool |

## Web Frontend (Next.js)

The web frontend (`src/web/nextjs-app/`) is a Next.js 16 application running on port 3000, served either via `next start` (standalone Docker image) or `next dev` (local development). It provides the user-facing UI: overview page, research chat (CopilotKit), dashboard metrics, and operator controls.

### Service Architecture

```
Next.js (port 3000)
  +-- /app/overview          — landing page with system overview
  +-- /app/research          — CopilotKit chat interface (proxied to orchestrator /a2a-agui)
  +-- /app/dashboard         — Langfuse metrics dashboard (KPIs, agent breakdown, latency charts)
  +-- /app/operator          — service health dashboard
  +-- /api/copilotkit         — POST proxy to orchestrator /a2a-agui
  +-- /api/dashboard         — GET aggregated dashboard metrics
  +-- /api/dashboard/scores  — GET RAGAS score timeseries
  +-- /api/reports/*         — GET report downloads (proxied to backend)
  +-- /api/auth/*            — login/logout endpoints
```

### Docker Build

Multi-stage Dockerfile (`src/web/nextjs-app/Dockerfile`):
1. **deps**: `node:20-alpine` + `npm ci`
2. **build**: `npm run build` (standalone output via `output: 'standalone'` in `next.config.ts`)
3. **runner**: `node:20-alpine` + standalone build output — no `node_modules`, ~120MB final image

### Langfuse API Integration

The dashboard queries Langfuse via its REST API. API limits are capped at 100 (Langfuse cloud tier limit) to prevent 400 errors. The dashboard and overview pages display actual API error messages rather than generic fallbacks.

## Health Endpoints

All eight services expose `GET /health`:

| Service | URL | Response |
|---|---|---|
| Web Frontend | `http://localhost:3000/api/health` | `{"status":"ok","agent":"web"}` |
| Orchestrator | `http://localhost:8001/health` | `{"status":"ok","agent":"orchestrator"}` |
| RAG Agent | `http://localhost:8002/health` | `{"status":"ok","agent":"rag"}` |
| Quant Agent | `http://localhost:8003/health` | `{"status":"ok","agent":"quant"}` |
| Market Context Agent | `http://localhost:8004/health` | `{"status":"ok","agent":"market_context"}` |
| Analytics Agent | `http://localhost:8005/health` | `{"status":"ok","agent":"analytics"}` |
| Reviewer Agent | `http://localhost:8006/health` | `{"status":"ok","agent":"reviewer"}` |
| MCP Server | `http://localhost:8010/health` | `{"status":"ok","agent":"mcp"}` |

### Eval Release Endpoints

All five sub-agent servers expose `POST /release-evals` to trigger deferred eval coroutines:

| Service | Endpoint | Response |
|---|---|---|
| RAG Agent | `POST http://localhost:8002/release-evals` | `{"released": N}` |
| Quant Agent | `POST http://localhost:8003/release-evals` | `{"released": N}` |
| Market Context Agent | `POST http://localhost:8004/release-evals` | `{"released": N}` |
| Analytics Agent | `POST http://localhost:8005/release-evals` | `{"released": N}` |
| Reviewer Agent | `POST http://localhost:8006/release-evals` | `{"released": N}` |

The orchestrator calls these endpoints via `_release_sub_agent_evals()` as a fire-and-forget background task after synthesis completes (5s HTTP timeout, non-fatal on failure).

The MCP server mounts its health route alongside the FastMCP SSE app via a Starlette wrapper in `get_app()`. Docker-compose `healthcheck` blocks use these endpoints with `curl -f`, and `depends_on` is set to `condition: service_healthy`.

## File Logging

All services write structured logs to the `logs/` directory via `src/shared/logging_config.py`:

```python
from shared.logging_config import setup_file_logging
setup_file_logging("orchestrator")  # → logs/orchestrator.log
```

`setup_file_logging(service_name)` attaches a `RotatingFileHandler` (10 MB max, 5 backups) and a `StreamHandler` to the root logger. Called at module level in each server entry point. Duplicate handler registration is guarded.

| Service | Log file |
|---|---|
| Orchestrator | `logs/orchestrator.log` |
| RAG Agent | `logs/rag_agent.log` |
| Quant Agent | `logs/quant.log` |
| Market Context Agent | `logs/market_context.log` |
| Analytics Agent | `logs/analytics.log` |
| Reviewer Agent | `logs/reviewer.log` |
| MCP Server | `logs/mcp.log` |

### Decorators

`src/shared/logging_config.py` exports two timing decorators:

- **`@logged`**: For async functions. Emits `Enter`/`Exit`/`Fail` structured log lines with `latency_ms` using `time.monotonic()` and `fn.__qualname__`. Applied to `GenericAgentExecutor.execute()`.
- **`@logged_sync`**: Same behaviour for synchronous functions.

### Third-party Logger Suppression

`setup_file_logging()` sets `httpx`, `chromadb`, `langfuse`, `hpack`, `urllib3`, and `asyncio` to `WARNING` by default. Each is overridable via a `LOG_LEVEL_<LIB>` env var:

```
LOG_LEVEL_HTTPX=DEBUG    # verbose HTTP traces
LOG_LEVEL_CHROMADB=INFO  # ChromaDB query details
```

### Operational Coverage

Key events emit structured log lines (visible in service log files):

- **Cache**: hit/miss/eviction in `src/shared/ttl_cache.py`
- **Sandbox**: entry/exit in `src/shared/code_sandbox.py`
- **Database**: open/close/migrate/prune in `src/shared/memory/store.py`
- **Memory**: brief store/update in `ticker_memory.py`; portfolio upsert in `portfolio_store.py`; recommendation record in `performance_tracker.py`
- **Reports**: format + byte count for each generated report in `api_routes.py`

### Entity-Relationship Diagram

FinSight uses two separate SQLite databases (since v2.6 — `db/finsight_memory.db` for business data, `db/adk_sessions.db` for conversation state). All tables live in the `db/` folder at the project root.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        finsight_memory.db                                │
│                                                                         │
│  ┌──────────────┐       ┌──────────────────┐       ┌────────────────┐  │
│  │ sessions      │──1:N──│ events            │       │ memory_entries │  │
│  │              │       │                   │       │                │  │
│  │ id (PK)      │       │ id (PK)           │──N:1─│ id (PK)        │  │
│  │ user_id      │       │ session_id (FK)   │       │ session_id (FK)│  │
│  │ created_at   │       │ event_type        │       │ content_hash   │  │
│  │ updated_at   │       │ data (JSON)       │       │ content        │  │
│  └──────────────┘       │ created_at        │       │ search_text    │  │
│                         └──────────────────┘       │ created_at     │  │
│                                                    └────────────────┘  │
│  ┌──────────────┐       ┌──────────────────┐                          │
│  │ ticker_briefs │       │ recommendation_  │                          │
│  │              │       │ records           │                          │
│  │ id (PK)      │       │ id (PK)           │                          │
│  │ ticker       │       │ ticker            │                          │
│  │ recommendation│       │ recommendation    │                          │
│  │ confidence   │       │ confidence        │                          │
│  │ response_text│       │ price_at_rec      │                          │
│  │ analysis_date│       │ created_at        │                          │
│  │ created_at   │       │ evaluated_at      │                          │
│  └──────────────┘       │ realized_return   │                          │
│                         └──────────────────┘                          │
│  ┌──────────────┐       ┌──────────────────┐       ┌────────────────┐  │
│  │ user_profiles │       │ ingested_filings  │       │ agent_output_  │  │
│  │              │       │                   │       │ store          │  │
│  │ id (PK)      │       │ edgar_url (PK)    │       │                │  │
│  │ user_id      │       │ ticker            │       │ session_id     │  │
│  │ holdings_json│       │ ingested_at       │       │ agent_name     │  │
│  │ risk_profile │       └──────────────────┘       │ output_json    │  │
│  │ investment_  │                                  │ created_at     │  │
│  │ horizon      │                                  └────────────────┘  │
│  │ updated_at   │                                                     │
│  └──────────────┘                                                     │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                          adk_sessions.db                                │
│                                                                         │
│  ┌──────────────┐       ┌──────────────────┐                          │
│  │ sessions      │──1:N──│ events            │                          │
│  │              │       │                   │                          │
│  │ id (PK)      │       │ id (PK)           │                          │
│  │ user_id      │       │ session_id (FK)   │                          │
│  │ app_name     │       │ author            │                          │
│  │ created_at   │       │ timestamp         │                          │
│  └──────────────┘       │ content (JSON)    │                          │
│                         └──────────────────┘                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Table Relationships

**Core conversation flow** (`adk_sessions.db`):
- `sessions` → `events`: One session has many events. Each event is a single turn (user message, agent response, tool call). The `data` JSON column stores the full event payload including LLM content parts and tool call/response metadata.

**Memory and persistence** (`finsight_memory.db`):
- `sessions` → `events`: Mirrors the ADK schema but used by the memory layer for cross-session search via `memory_entries`.
- `sessions` → `memory_entries`: One session has many memory entries. `content_hash` enables deduplication. `search_text` is the FTS5-indexed column queried by `load_memory`.
- `ticker_briefs`: Independent table keyed by ticker + analysis_date. Each row is a complete BUY/HOLD/SELL analysis. Linked to the orchestrator's `save_brief` flow — not directly to sessions.
- `recommendation_records`: Tracks the same recommendation with a `price_at_rec` snapshot. Updated asynchronously by `PerformanceTracker` when evaluation completes, populating `evaluated_at` and `realized_return`.
- `user_profiles`: One row per `user_id`. Stores portfolio holdings as JSON (`holdings_json`), risk tolerance, and investment horizon. Updated by `PortfolioStore.update_holdings()`.
- `ingested_filings`: Deduplication table for RAG ingestion. `edgar_url` is the primary key (SEC URLs are immutable and canonical).
- `agent_output_store`: Cross-process data bridge. Keyed by `(session_id, agent_name)`. The orchestrator writes sub-agent outputs here via `store_agent_output()`; the reviewer reads them via `get_agent_outputs()`.

### Data Lifecycle

```
User Query (e.g. "Should I invest in NVDA?")
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 1. INGESTION & ROUTING (Orchestrator)                        │
│    • Extract ticker via regex + MCP validate_ticker          │
│    • Check same-day cache (ticker_briefs.analysis_date)      │
│    • Check semantic cache (ChromaDB cosine similarity)        │
│    • Inject memory context (ticker_briefs + user_profiles)   │
│    • LLM routes to sub-agents via send_message               │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. ANALYSIS (Sub-agents, parallel)                          │
│    • RAG: fetch SEC filings → ChromaDB → LlamaIndex query    │
│    • Quant: MCP prices/financials → LangGraph computation    │
│    • Market: MCP macro/peers → CrewAI narrative              │
│    • Analytics: MCP prices → PydanticAI DAG (trend/forecast/ │
│      anomaly/stats/charts)                                   │
│    • Each agent writes its own output to agent_output_store   │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. REVIEW (Reviewer Agent, sequential)                       │
│    • Fetch agent outputs from agent_output_store             │
│    • Run 6 deterministic validation tools (Python)           │
│    • LLM synthesizes structured review + meta-confidence     │
│    • Output: verdict, contradictions, confidence_breakdown   │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. PERSISTENCE (Orchestrator after_agent_callback)           │
│    • save_brief() → ticker_briefs (full synthesis text)      │
│    • PortfolioStore.update_holdings() → user_profiles        │
│    • PerformanceTracker.record_recommendation() →            │
│      recommendation_records (with price_at_rec snapshot)     │
│    • add_events_to_memory() → memory_entries (FTS5 indexed)  │
│    • _release_sub_agent_evals() → POST /release-evals        │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 5. EVALUATION (Background, fire-and-forget)                  │
│    • Each agent's score_*_response() runs RAGAS metrics      │
│    • Scores pushed to Langfuse under rags/{agent}/{metric}   │
│    • PerformanceTracker evaluates past recommendations       │
│      against current prices → recommendation_records          │
│      (realized_return, evaluated_at)                         │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 6. PRUNING (Startup + background)                            │
│    • prune_old_records(): delete ticker_briefs,               │
│      recommendation_records, memory_entries older than        │
│      MEMORY_RETENTION_DAYS=90                                │
│    • prune_stale_outputs(): delete agent_output_store        │
│      entries older than 600s                                 │
│    • ChromaDB semantic cache: entries never expire (date-     │
│      scoped queries prevent stale reads)                     │
└─────────────────────────────────────────────────────────────┘
```

### Schema DDL

```sql
-- Conversation state (adk_sessions.db)
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    app_name TEXT DEFAULT 'orchestrator',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    author TEXT NOT NULL DEFAULT 'orchestrator',
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    content JSON NOT NULL,          -- Array of {type, text, name, args, ...}
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- Memory and persistence (finsight_memory.db)
CREATE TABLE ticker_briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    recommendation TEXT NOT NULL,   -- BUY/HOLD/SELL
    confidence REAL,                -- 0.0-1.0
    response_text TEXT,             -- Full LLM synthesis (up to 4000 chars)
    analysis_date TEXT,             -- YYYY-MM-DD (IST timezone)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE recommendation_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    confidence REAL,
    price_at_rec REAL,              -- yfinance snapshot at recommendation time
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    evaluated_at TIMESTAMP,         -- NULL until performance eval runs
    realized_return REAL            -- NULL until evaluated
);

CREATE TABLE user_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL UNIQUE,
    holdings_json TEXT,             -- {"AAPL": 10, "MSFT": 5, ...}
    risk_profile TEXT,              -- conservative/moderate/aggressive
    investment_horizon TEXT,        -- short/medium/long
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE memory_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,     -- SHA-256 for dedup
    content TEXT NOT NULL,          -- Original event content
    search_text TEXT NOT NULL,      -- FTS5-indexed plain text
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE ingested_filings (
    edgar_url TEXT PRIMARY KEY,     -- Canonical SEC URL (immutable)
    ticker TEXT NOT NULL,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE agent_output_store (
    session_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,       -- rag, quant, market_context, analytics, reviewer
    output_json TEXT NOT NULL,      -- Full agent output as JSON
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, agent_name)
);

-- Indexes
CREATE INDEX idx_ticker_briefs_ticker ON ticker_briefs(ticker);
CREATE INDEX idx_ticker_briefs_date ON ticker_briefs(analysis_date);
CREATE INDEX idx_events_session ON events(session_id);
CREATE INDEX idx_memory_session ON memory_entries(session_id);
CREATE INDEX idx_memory_hash ON memory_entries(content_hash);
CREATE INDEX idx_recommendation_ticker ON recommendation_records(ticker);
CREATE INDEX idx_ingested_ticker ON ingested_filings(ticker);
```

### Key Design Decisions

- **Two databases**: `adk_sessions.db` is owned by the ADK framework (conversation state); `finsight_memory.db` is owned by FinSight's memory layer (business data). Separating them prevents schema conflicts (v2.6).
- **`analysis_date` vs `created_at`**: `ticker_briefs` uses a separate `analysis_date` (YYYY-MM-DD) for same-day cache comparisons. This avoids timezone ambiguity — `created_at` is a full ISO-8601 timestamp that requires parsing to determine the day boundary.
- **`agent_output_store` as cross-process bridge**: Rather than passing full agent outputs through A2A message payloads (size limits, serialization overhead), the orchestrator writes them to SQLite and the reviewer reads by `session_id`. The 600s TTL keeps the table small.
- **`content_hash` dedup in memory_entries**: SHA-256 hash of the content prevents duplicate entries when the same session is processed multiple times (e.g., retry after failure). FTS5 indexing on `search_text` enables fast full-text search for `load_memory`.
- **`ingested_filings` dedup**: SEC EDGAR URLs are canonical and immutable — using `edgar_url` as primary key makes re-ingestion checks a simple indexed lookup.

### HF_HUB_OFFLINE

`src/shared/bootstrap.py` sets `HF_HUB_OFFLINE=1` before any HuggingFace code runs (moved from `src/shared/config.py` in v1.41). This prevents network calls to `huggingface.co` when loading `sentence-transformers` or embedding models — models must be cached locally from a prior online run. Set `HF_HUB_OFFLINE=0` in `.env` to re-enable download checks.

## Runtime RAGAS Evaluation

After each agent produces a response, a fire-and-forget background task scores it using RAGAS metrics that require no ground-truth reference. Scores are pushed to Langfuse per-trace (linked by `trace_id`).

### Feature flag

All sidecar evals are gated by `EVAL_TRACE_ENABLED` in `.env` (default `True`). The flag is exposed as `EVAL_ENABLED` in `shared.settings`; every agent's `asyncio.create_task(_eval_*)` call site checks it. Set `EVAL_TRACE_ENABLED=False` to disable all per-agent runtime scoring with no code changes — useful for fast iteration when LM Studio judge calls add 5—180s of background work per query.

### Orchestrator eval hook lives in `after_agent_callback`

When the orchestrator runs through `adk web` (the path `run_adk_web.bat` uses), the ADK Web runner is responsible — `FinSightAgentExecutor` is never invoked. The orchestrator's eval is therefore scheduled from `src/orchestrator/web/agent.py`'s `_persist_memory_callback`, not from `agent_executor.py`. The callback first runs `_is_analysis_turn(session.events)`: if `save_brief` was not called in this turn (e.g. the user only asked "what were my last recommendations?"), both memory persist and eval are skipped to avoid polluting long-term memory with conversational queries.

`FinSightAgentExecutor` still keeps its eval call for completeness — it fires when an A2A client hits `src/orchestrator/main.py` directly. The A2A server is not started by `run_adk_web.bat` by default; start it manually with `uv run python -m orchestrator.main` if needed.

| Agent | Background Task | Metrics | Why Each Metric | Data Required |
|---|---|---|---|---|
| Orchestrator | `score_response()` | AnswerRelevancy, citation_quality, risk_disclosure, recommendation_clarity, response_completeness, no_forward_guarantees | AnswerRelevancy: generic catch-all for response quality. citation_quality: unsubstantiated financial claims are worthless — must cite filing dates/amounts. risk_disclosure: an investment thesis without risk discussion is incomplete. recommendation_clarity: the core output is a BUY/HOLD/SELL signal — ambiguous synthesis fails. response_completeness: must synthesize all analysis types, not just one. no_forward_guarantees: flags any language suggesting guaranteed future performance. | `user_input`, `response` |
| RAG | `score_rag_response()` | Faithfulness, ContextPrecisionWithoutReference, cross_collection_synthesis | Faithfulness: prevents hallucinated dates/numbers by verifying claims against retrieved SEC text. ContextPrecisionWithoutReference: flags retrieval drift — when RAG returns irrelevant filings, this drops even if Faithfulness passes. cross_collection_synthesis: checks whether the response cites sources from ≥2 collections (sec_filings, news, earnings). | `user_input`, `response`, `context_texts` (ChromaDB nodes) |
| Quant | `score_quant_response()` | FactualCorrectness, signal_explanation_quality, deterministic (schema validator) | FactualCorrectness: compares LLM summary numbers (Sharpe, VaR, DCF) against actual computed values — primary failure mode is hallucinated numbers. signal_explanation_quality: scores whether the response explains three or more signal groups with specific numeric values. deterministic: zero-LLM schema validation — checks all 8 signal groups, weight sum, MC percentiles, peer fields, recommendation invariants. | `user_input`, `response`, `quant_result` (computed metrics dict) |
| Market Context | `score_market_context_response()` | Faithfulness, macro_regime_analysis, peer_landscape_quality | Faithfulness: verifies narrative is grounded in collected macro and peer data. macro_regime_analysis: evaluates if narrative discusses yield curve, VIX, DXY, sector ETF performance with actual values. peer_landscape_quality: evaluates depth of peer comparison across multiple metrics. | `user_input`, `response`, `_retrieved_contexts` (macro + peer data) |
| Analytics | `score_analytics_response()` | Runtime RAGAS eval + deterministic schema checks | Validates analytics output schema and defers LLM-based eval for trend, forecast, and anomaly quality. | `user_input`, `response`, analytics result |
| Reviewer | `score_reviewer_response()` | Runtime RAGAS eval + deterministic schema checks | Validates reviewer output schema and defers LLM-based eval for review quality. | `user_input`, `response`, reviewer result |

### Per-Metric Streaming

Metrics within an agent are run concurrently via `asyncio.wait(FIRST_COMPLETED)` instead of `asyncio.gather`. Each metric score is logged and pushed to Langfuse the moment its `ascore()` finishes — fast metrics (AnswerRelevancy, DomainSpecificRubrics ~3-5s) appear immediately without waiting for slow metrics (e.g., Faithfulness which runs multiple sequential LLM calls and can take ~180s).

### Client Caching

`_setup_ragas_clients()` caches the `(InstructorLLM, _STEmbeddings)` tuple at module level after the first call. All six agents reuse the cached `SentenceTransformer` model — the previous approach loaded a fresh model (~1-2s, ~80MB) on every agent response, multiplying latency by 6 per query.

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

These metrics require a human-curated reference dataset and live in `src/tests/evaluation/` as offline scripts:
- `run_rag_eval.py` — Faithfulness, ResponseRelevancy, ContextPrecision, ContextRecall, NoiseSensitivity
- `run_orchestrator_eval.py` — ToolCallAccuracy, AgentGoalAccuracy

### Auto-Save Flow

After each successful response, the executor automatically persists:

```
Response Complete → Executor._auto_save_memory(query, response)
  +-- extract_ticker(query) → "NVDA"
  +-- TickerMemory.store_brief(ticker, recommendation, response)
  +-- PortfolioStore.update_holdings(extracted_holdings)
  +-- PerformanceTracker.record_recommendation(ticker, rec, confidence)
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
| `src/orchestrator/main.py` | Initializes `DatabaseSessionService` and `SQLiteMemoryService` |
| `src/orchestrator/agent_executor.py` | Memory context injection, auto-save, `_add_to_memory` |
| `src/orchestrator/agent.py` | System prompt includes memory usage instructions |

### Database Files

All databases are stored under the `db/` folder at the project root — the entire folder is excluded from git via `.gitignore`.

- `db/finsight_memory.db` — ticker briefs, portfolios, performance records, ingested filings
- `db/adk_sessions.db` — ADK conversation sessions and events (separated from memory data in v1.24 to prevent schema conflicts)
- `db/chroma_db/` — ChromaDB vector store for SEC filing RAG and semantic cache
- `db/.langchain_cache.db` — LangChain SQLiteCache for quant agent LLM responses
- All files auto-created on first run; `db/` directory created by `get_db()` via `path.parent.mkdir(parents=True, exist_ok=True)`

## Report Generation

Report generation is a separate subsystem in `src/shared/reports/` package (split from the monolithic `src/shared/report_generator.py` in v1.41) that produces two output formats (HTML, PDF) from the same shared data extraction pipeline. It is invoked via HTTP API routes in `src/orchestrator/api_routes.py`.

```
shared/reports/
  ├── __init__.py        # public API re-exports generate_html/pdf
  ├── deck_model.py      # DeckData dataclass with 16+ typed fields
  ├── extraction.py      # _extract_deck_data() pipeline with robust regex + Pydantic models
  ├── playwright_export.py # Playwright-based PDF export (A4 portrait)
  └── html_renderer.py   # Jinja2 template rendering (scrollable page)
```

Back-compat shim `src/shared/report_generator.py` was removed in v2.0 (Phase 3.5).

### API Routes — Backend (Starlette App)

| Route | Method | Format | Description |
|---|---|---|---|
| `/api/reports/ticker/{symbol}/latest/{format}` | GET | html, pdf | Generate report for ticker's latest brief |
| `/api/reports/{brief_id}/{format}` | GET | html, pdf | Generate report from a specific brief by ID |

Route ordering is significant: `/ticker/{symbol}/latest/{format}` must be declared before `/{brief_id}/{format}` in the Starlette route list. Otherwise `/{brief_id}` captures `"ticker"` and `{format}` captures `"{symbol}"`.

### API Routes — Frontend (Next.js)

| Route | Method | Description |
|---|---|---|
| `/api/copilotkit` | POST | Main chat interface — proxies to orchestrator's AG-UI bridge at `/a2a-agui` |
| `/api/dashboard` | GET | Dashboard metrics — KPIs, agent breakdown, latency timeseries (`?hours=24`) |
| `/api/dashboard/scores` | GET | RAGAS quality scores per agent (timeseries for chart rendering) |
| `/api/reports/ticker/{symbol}/latest/{format}` | GET | Generate report for ticker's latest brief (proxied to backend) |
| `/api/auth/login` | POST | User login — JWT returned as `finsight_session` cookie |
| `/api/auth/logout` | POST | Clear session cookie |
| `/api/health` | GET | Health proxy — `?svc=orchestrator\|rag\|quant\|market\|analytics\|reviewer\|mcp` |

### Data Flow

```
HTTP Request → api_routes.py handler
  → _load_brief_data(brief_id_or_symbol)  — loads from TickerMemory
  → generate_html / generate_pdf_async
    → _extract_deck_data(brief)  — shared pipeline (Pydantic models)
      +-- _extract_metric(deck, "sharpe_ratio")   → 1.45
      +-- _extract_recommendation(brief)           → "BUY"
      +-- _extract_scorecards(metrics)             → Momentum, RSI scorecards
      +-- _extract_advanced_scorecard(metrics)     → VaR, beta, volatility
      +-- _extract_fundamentals(metrics)           → PE, ROE, margins
      +-- _extract_executive_sections(brief)       → [Price Target, Thesis, Recommendation]
      +-- _populate_from_agent_outputs(brief)      → Pydantic model parsing
      +-- _extract_holdings(metrics)               → portfolio table
    → format-specific generator (HTML scrollable page or A4 PDF)
```

### Format-Specific Generators

**HTML** (`generate_html`):
- Uses `_get_jinja_env()` — lazy-loaded `Jinja2` `Environment` with `FileSystemLoader("shared/templates/")`
- Renders `investment_deck.html` template as a full scrollable page (v2.5 replaced the slide deck format):
  - Sticky PDF download bar at top, responsive layout
  - Section-based layout with `break-inside-avoid` for PDF print CSS
  - All sections rendered conditionally; empty-data sections are omitted
- Full HTML includes CSS styles and is self-contained with zero external dependencies
- Returns `text/html` with `Content-Type: text/html`

**PDF** (`generate_pdf_async`):
- Uses Playwright in print-mode (`page.pdf()`) to render the scrollable HTML as A4 portrait
- Print CSS: cover page, section breaks, conclusion back page
- Margins: 18mm top, 16mm right, 20mm bottom, 16mm left
- Falls back to HTML download when Playwright is unavailable
- Returns `Content-Type: application/pdf`

### Playwright Export (`src/shared/reports/playwright_export.py`)

- `html_to_pdf(html_content, output_path)`: Launches headless Chromium, renders scrollable HTML, calls `page.pdf()` with A4 portrait dimensions and print media type. Margins: 18/16/20/16mm.
- Uses `asyncio.new_event_loop()` on Windows to avoid ProactorEventLoop incompatibility with Playwright's subprocess management
- Graceful fallback: when Playwright is not installed or Chromium is unavailable, functions log a warning and return raw HTML bytes, allowing the caller to serve HTML directly

### Agent Output Capture

The orchestrator captures parsed sub-agent responses at the `send_message` tool level and stores them in brief metadata for structured report generation. Both A2A and ADK Web UI paths now capture agent responses consistently:

```
A2A path (agent_executor.py):
  send_message tool callback
    → Extract response text from A2A artifact
    → Parse into structured fields (quant metrics, RAG summary, sentiment narrative)
    → Store in session event metadata via extra_data parameter
    → store_minimal() persists extra_data as JSON in brief record

ADK Web UI path (web/agent.py):
  after_agent_callback
    → _collect_agent_extra() — pop agent responses from send_message events
    → Map into brief_json keys via update_brief_json()
    → Recommendation/confidence columns updated on same-day re-analysis
    → Previously, structured agent data was silently lost on every ADK web query
```

This enables `_populate_from_agent_outputs()` in the extraction pipeline to build report sections from structured data instead of parsing prose, improving extraction accuracy for metrics, scorecards, and peer comparisons.

### Shared Components

| Component | File | Purpose |
|---|---|---|
| `_extract_deck_data()` | `src/shared/reports/extraction.py` | Shared extraction pipeline — returns `DeckData` dataclass |
| `_populate_from_agent_outputs()` | `src/shared/reports/extraction.py` | Extracts report data from structured agent responses (Pydantic models) |
| `_truncate_at_sentence()` | `src/shared/reports/extraction.py` | Sentence-aware truncation for executive summary and market narrative |
| `_rsi_status()` | `src/shared/reports/extraction.py` | Classifies RSI (Overbought/Bullish/Neutral/Oversold) |
| `_get_jinja_env()` | `src/shared/reports/html_renderer.py` | Lazy-loaded Jinja2 environment for HTML templates |
| `html_to_pdf()` | `src/shared/reports/playwright_export.py` | Playwright print-mode HTML → A4 PDF export |
| `investment_deck.html` | `src/shared/templates/` | Scrollable HTML page template (replaced slide deck in v2.5) |
