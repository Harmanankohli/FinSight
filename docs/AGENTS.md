# Agents Reference

## Agent 1: Orchestrator (ADK)

| Property | Value |
|---|---|
| Framework | Google ADK |
| Port | 8001 |
| Agent Card | Built programmatically in `agent_1_adk/main.py` |
| Discovery | `A2ACardResolver` via `/.well-known/agent-card.json`, async with 3x retry |
| A2A Endpoint | `POST /a2a` (via Starlette + `create_jsonrpc_routes`) |
| Health | `GET /health` → `{"status":"ok","agent":"orchestrator"}` |

The orchestrator uses a single `LlmAgent` with one `send_message` tool. The LLM delegates tasks to sub-agents by name and synthesizes results:

1. **Discovers agents in background** via `SubAgentClient.discover()` — async `A2ACardResolver` with retries
2. **Input guardrails** — Off-topic queries rejected in < 100 ms via `_NON_INVESTMENT_RE`. Invalid tickers rejected in < 2 s via pre-flight MCP `validate_ticker` call before any sub-agent is invoked. Temporary MCP connection for validation is cleaned up in a `finally` block via `await _mcp.disconnect_all()`.
3. **Semantic cache** — When `SEMANTIC_CACHE_ENABLED=true`, similar queries (cosine ≥ 0.95) return cached responses without running the orchestrator.
4. **Memory cache callback (before agent)** — `root_agent.before_agent_callback = _memory_cache_callback` fires before the LLM runs. Extracts the user's ticker, queries `TickerMemory.get_latest()`, and if today's brief exists with a valid `response_text`, returns it as `types.Content` — short-circuiting the LLM entirely. This is the fastest path for same-day repeat queries (~200ms vs 30-60s).
5. **Memory context injection** — If the cache callback misses (no today brief), extracts ticker from user input, retrieves latest recommendation from `TickerMemory`, and prepends it to the user message (~300 token budget). Tags as `[TODAY]` (fresh) or `[STALE]` (prior day) — LLM is instructed **"MUST return directly"** on `[TODAY]` rather than the previous "MAY return".
6. **LLM routes to agents in parallel** — The `_STATIC_PREAMBLE` explicitly instructs the model to emit ALL `send_message` calls in a SINGLE assistant response for parallel execution (`agent.py:184-189`). Qwen3-30B-A3B natively supports multiple tool calls in one turn. The `_build_instruction()` also appends agent responsibility boundaries (`agent.py:253-260`) clarifying RAG owns ALL document/news retrieval, Sentiment provides macro context, Quant owns numeric analysis — preventing the LLM from routing news queries to the wrong agent. Each call measured with `time.monotonic()` and emitted as a Langfuse latency span.
7. **Output guardrails** — Responses shorter than 50 chars trigger `TASK_STATE_FAILED`. Missing BUY/HOLD/SELL signal emits a Langfuse warning with `missing_signal: true`.
8. **Synthesizes results** — LLM collects all outputs and produces a BUY/HOLD/SELL recommendation
9. **Auto-save** — After each response, persists ticker brief, portfolio holdings, and performance record (with live price snapshot via yfinance) to SQLite. Fires background task to evaluate past recommendations.
10. **Memory persist + Runtime RAGAS evaluation** — All run from `after_agent_callback` in `agents/finsight_agent/agent.py`. The callback first checks `_is_analysis_turn()` (was `save_brief` called in this turn?) — non-analysis turns like "what were my last recommendations?" skip persist + eval to avoid memory pollution. Analysis turns then call `add_events_to_memory()` and fire `asyncio.create_task(_eval_score_response(...))` with 5 metrics. The full synthesis text is stored directly by `save_brief` (reads longest LLM text from session events) — the post-turn `update_response_text` overwrite was removed. All gated globally by `EVAL_TRACE_ENABLED`. Scores pushed to Langfuse under `ragas/orchestrator/<metric>`.

All A2A communication uses `ClientFactory` + `BaseClient` from the official `a2a-sdk`. Streaming events are handled correctly: intermediate SUBMITTED/WORKING events are skipped, only `artifact_update` events (data or text) and terminal `status_update` events are returned to the LLM.

### Architecture

```
Module load → SubAgentClient.discover() in background task
  ├── A2ACardResolver(http, "http://localhost:8002")
  ├── A2ACardResolver(http, "http://localhost:8003")
  └── A2ACardResolver(http, "http://localhost:8004")
  → self.agents populated → instruction updated

FinSightAgentExecutor:
  A2A Request → execute()
  → _build_memory_context(query) → inject [MEMORY CONTEXT] prefix
  → RUNNER.run_async(user_query)
  → ADK LlmAgent (tools: [send_message, save_brief, load_memory])
    → LLM emits ALL send_message calls in ONE assistant turn (parallel, per system prompt instruction)
    → send_message("Financial RAG Agent", "Analyze NVDA...")
    → send_message("Quant Analysis Agent", "Compute metrics for NVDA...")
    → send_message("Market Context Agent", "Sentiment for NVDA...")
    → LLM receives all results together in the next turn
    → LLM synthesizes BUY/HOLD/SELL
    → load_memory(query="...") — search past conversations
  → _add_events_to_memory() → get_session() → add_session_to_memory()
  → _persist_to_memory() → direct events → SQLiteMemoryService (for load_memory)
  → _store_memory() → TickerMemory + PortfolioStore + PerformanceTracker

before_agent_callback (ADK Web UI path — fires before LLM every turn):
  → _memory_cache_callback(callback_context)
       → Extract user ticker from session.events (regex)
       → TickerMemory.get_latest(ticker, user_id=None)
         ├── hit → return types.Content directly (short-circuit)
         ├── miss → fall back to MCP resolve_company_ticker for company-name tokens
         │          (e.g. "VISA" → canonical "V") then retry cache lookup
         └── still miss → return None (let LLM run)

Executor-level cache (A2A path — agent_1_adk/agent_executor.py):
  A2A Request → execute()
  → _get_today_cached_text(ticker) — same check, returns cached text directly
  → [miss] → _build_memory_context() → RUNNER.run_async()

after_agent_callback (ADK web UI path — primary path; run_adk_web.bat no longer starts agent_1_adk/main.py):
  → _is_analysis_turn(session.events) — was save_brief called?
       ├── No  → skip persist + eval (memory recall turn, e.g. load_memory only)
       └── Yes →
            → callback_context.add_events_to_memory(events=session.events, custom_metadata={...})
            → SQLiteMemoryService.add_events_to_memory() → memory_entries table
            → if EVAL_ENABLED: asyncio.create_task(score_response(query, response, trace_id))
                 → ragas/orchestrator/{AnswerRelevancy, citation_quality, risk_disclosure,
                                       recommendation_clarity, response_completeness}
```

### Streaming Event Flow

When `send_message` is called, sub-agents respond with streaming events:

```
event 1: task { state: SUBMITTED }          ← skipped (non-terminal)
event 2: status_update { WORKING, msg }     ← skipped (non-terminal)
event 3: artifact_update { data: {...} }    ← returned (actual result)
    OR status_update { COMPLETED, msg }     ← returned (terminal text)
    OR task { state: COMPLETED, artifacts } ← returned (non-streaming fallback)
```

### Sample Request

```json
POST /a2a
Headers: {"A2A-Version": "1.0"}
Body:
{
  "jsonrpc": "2.0",
  "method": "SendMessage",
  "id": "abc123",
  "params": {
    "message": {
      "messageId": "def456",
      "role": "ROLE_USER",
      "parts": [{"text": "Should I invest in NVDA?"}]
    }
  }
}
```

### Runtime Evaluation

Triggered from `after_agent_callback` (ADK Web path) — see step 9 above. Fires `asyncio.create_task(score_response(...))` with 5 metrics. All scored from `user_input` + `response` only — no ground-truth reference needed. Globally toggled by `EVAL_TRACE_ENABLED` in `.env`. Scores pushed to Langfuse under `ragas/orchestrator/<metric>`.

| Metric | Why |
|---|---|
| `AnswerRelevancy` | Measures whether the final BUY/HOLD/SELL recommendation addresses what the user asked. Generic catch-all. |
| `citation_quality` (DomainSpecificRubrics) | Custom 5-level rubric: scores whether the response cites specific filing dates, sections, and monetary figures vs making generic claims. A response saying "NVDA's revenue grew" without citing the 10-Q date and amount scores 1. Critical for financial credibility — unsubstantiated claims are worthless. |
| `risk_disclosure` (DomainSpecificRubrics) | Custom 5-level rubric: evaluates whether risks are acknowledged. An investment thesis without risk discussion is incomplete. Scores from "no risk mentioned" (level 1) to "balanced multi-category risk assessment" (level 5). |
| `recommendation_clarity` (DomainSpecificRubrics) | Custom 5-level rubric: verifies the synthesizer produces an explicit BUY/HOLD/SELL signal with supporting evidence from ≥2 sub-agents. A response that discusses pros/cons without committing to a clear signal scores low. |
| `response_completeness` (DomainSpecificRubrics) | Custom 5-level rubric: assesses whether all three analysis types (SEC filings, quant metrics, sentiment) are synthesized. A response that only discusses stock price without filing data or narrative scores 1. |

---

## Agent 2: RAG (LlamaIndex)

| Property | Value |
|---|---|
| Framework | LlamaIndex |
| Executor | `GenericAgentExecutor(RAGAgent)` |
| LLM | LM Studio via `llama-index-llms-openai-like` |
| Vector Store | ChromaDB (local, persisted to `./db/chroma_db`) |
| Embeddings | HuggingFace `all-MiniLM-L6-v2` (local) |
| Port | 8002 |
| Agent Card | Built programmatically in `agent_2_llamaindex/server.py` |
| A2A Endpoint | `POST /a2a` |
| Health | `GET /health` → `{"status":"ok","agent":"rag"}` |

### Skills

| ID | Name | Description |
|---|---|---|
| `sec_filing_retrieval` | SEC Filing Retrieval | Retrieves and analyzes SEC 10-K, 10-Q, 8-K filings |
| `earnings_summary` | Earnings Summary | Summarizes earnings call transcripts |
| `financial_news_retrieval` | Financial News Retrieval | Retrieves and analyzes recent financial news articles with sentiment context for impact assessment on stock prices |

### Architecture

```
Request → DefaultRequestHandler → GenericAgentExecutor(RAGAgent)
  → RAGAgent.stream(query, context_id, task_id)
    → try: yield await self._build_response(query)
    → finally: await self._disconnect()

The core response logic lives in _build_response(query) → dict, extracted from stream() in v1.24:

RAGAgent._build_response(query):
    → extract_trace_ids(query)
    → with langfuse.start_as_current_observation(name="rag-agent-stream")
      → ticker = extract_ticker(query)  — supports dotted (BRK.A), single-char (V), $prefix
      → Fallback chain: regex → MCP resolve → MCP validate
      → asyncio.create_task(self._ensure_ingested(ticker))      (Phase 2 — fire-and-forget)
      → asyncio.create_task(self._ensure_news_ingested(ticker))
      → FinancialIndexManager.query(ticker, query)  — returns immediately from indexed data
        ├── _classify_query_intent() → sec_filings ∪ news ∪ earnings
        ├── Multi-collection retrieval with score-sorted dedup
        └── LlamaIndex response synthesizer
      → if EVAL_ENABLED: asyncio.create_task(score_rag_response(...))
      → return {response_type: "data", content: result, ...}

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

#### Runtime Evaluation

After each response, fires `asyncio.create_task(score_rag_response(...))` with 3 metrics. Scored from `user_input`, `response`, and `context_texts` (ChromaDB source chunks). No ground-truth reference required.

| Metric | Why |
|---|---|
| `Faithfulness` | Verifies every claim in the response is directly supported by the retrieved SEC filing text. Prevents hallucinated dates, numbers, or citations. |
| `AnswerRelevancy` | Measures whether the response answers what the user asked. Generic catch-all for response quality. |
| `ContextPrecisionWithoutReference` | Evaluates whether the retrieved ChromaDB chunks are relevant to the query. Flags retrieval drift — when RAG returns irrelevant filings, this drops even if Faithfulness passes. |

---



## Agent 3: Quant (LangGraph)

| Property | Value |
|---|---|
| Framework | LangChain + LangGraph |
| Executor | `GenericAgentExecutor(QuantAgent)` |
| LLM | LM Studio via `langchain-openai` (summary node only) |
| LLM Cache | LangChain `SQLiteCache` — identical inputs reuse cached LLM response |
| Data Source | MCP (finsight-mcp `get_prices`, `get_financials`) |
| Port | 8003 |
| Agent Card | Built programmatically in `agent_3_langgraph/server.py` |
| A2A Endpoint | `POST /a2a` |
| Health | `GET /health` → `{"status":"ok","agent":"quant"}` |

### Skills

| ID | Name | Description |
|---|---|---|
| `quant_analysis` | Quantitative Analysis | Compute Sharpe ratio, Beta, VaR, volatility, DCF valuation, stress tests |
| `options_flow_analysis` | Options Flow Analysis | Analyze put/call volume and open interest ratios to detect unusual options activity |
| `insider_transaction_analysis` | Insider Transaction Analysis | Track Form 4 filings, detect cluster buying/selling patterns by executives |
| `positioning_signals` | Positioning Signals | Evaluate short interest, analyst consensus, earnings surprise history, and squeeze risk |

### Architecture

```
Request → DefaultRequestHandler → GenericAgentExecutor(QuantAgent)
  → QuantAgent.stream(query, context_id, task_id)
    → try: yield await self._build_response(query)
    → finally: await self._disconnect()

QuantAgent._build_response(query):
    → extract_trace_ids(query)
    → with langfuse.start_as_current_observation(name="quant-agent-stream")
      → extract_holdings(query, exclude_ticker=ticker) → ["AAPL", "MSFT", "GOOGL"]
      → QuantAgent.analyze(ticker, portfolio_holdings=holdings)
        [Parallel fan-out from START — Phase 4 adds 3 behavioral nodes]
          ├── fetch_prices → compute_metrics ∥ technical_analysis
          │     (SMA, MACD, RSI, Bollinger, support/resistance, trend)
          │     → volatility gate → stress_test (beta-adjusted) XOR dcf_valuation
          │         (data-driven WACC via CAPM + CoD, tapered growth)
          │     → monte_carlo (GBM, 10k paths, 60-day horizon)
          ├── fetch_fundamentals → 25+ ratios (PE, ROE, margins, D/E, etc.)
          │     + derived signals (golden cross, net debt, 52w extremes)
          │     → peer_comparison (ranks vs peers on PE, EV/EBITDA, growth, margins, ROE)
          ├── options_flow_node (put/call vol ratio, OI ratio, flow signal)
          ├── insider_signals_node (Form 4 90-day, net direction, CEO/CFO weight)
          └── analyst_positioning_node (consensus, upside %, short interest, squeeze)
        → portfolio_correlation
        → format_output (8-group weighted voting, sum=1.0)
          (technical 0.15, fundamental 0.15, narrative 0.10, options 0.12,
           insider 0.10, positioning 0.11, macro 0.12, risk 0.15)
        → llm_summary (enriched 3-4 sentence summary)
      → if EVAL_ENABLED: asyncio.create_task(score_quant_response(...))
      → return {response_type: "data", content: result, ...}
```

**Portfolio Holdings Extraction**: `extract_holdings()` in `shared/ticker_utils.py` uses regex patterns to extract holdings from natural language (e.g. "My portfolio holds AAPL, MSFT, GOOGL"). The orchestrator LLM is instructed to include holdings in the task text for the Quant agent. Holdings are passed through the full chain and used by `correlation_node` to compute a correlation matrix.

**Correlation Matrix Notes**: When no holdings are provided, returns `{"note": "No portfolio holdings provided..."}` instead of `{}`.

**Logging & Error Propagation**: The graph logs routing decisions, metric computation failures, DCF fallbacks, and beta calculation errors. When DCF is skipped due to high volatility, the `dcf_error` field is set and propagated through formatting and LLM summary, giving users visibility into why DCF was not computed.

**Monte Carlo Simulation**: `_run_monte_carlo()` at `nodes.py:42` runs 10,000 geometric Brownian motion paths over a 60-day horizon. Returns p10/p25/p50/p75/p90 percentiles, probability of profit, and MC VaR(95). Stored in `QuantAnalysisState.monte_carlo`.

**8-Group Weighted Voting**: `_SIGNAL_WEIGHTS` at `nodes.py:107-117` sum to 1.0 across 8 groups. Confidence = `|composite| × (1 − std(present_signals))`. Raw weighted sum (not normalized) — signal density naturally reduces composite when fewer signals are present.

**Shared Peer Resolver**: `peer_comparison_node` and the Market Context Agent both use `shared/peer_sets.py` — 33 hand-curated peer sets across 10+ sectors. The peer rank node fetches peer financials and computes percentile ranks on PE, EV/EBITDA, RevGrowth, OpMargin, ROE.

#### Runtime Evaluation

After each analysis, fires `asyncio.create_task(score_quant_response(...))` with 4 metrics. Scored from `user_input`, `response`, and full `quant_result` dict (Sharpe, VaR, DCF, beta, MC, signals) serialized as a reference string. `AnswerRelevancy` removed from Quant (kept on Orchestrator only, v1.31).

| Metric | Why |
|---|---|
| `FactualCorrectness` | Compares the LLM summary's numerical claims against the actual computed metrics. The reference is the raw quant result dict — if the LLM says "Sharpe ratio of 1.5" but the computation produced 0.8, this metric catches it. Prevents hallucinated numbers, the primary failure mode for quantitative analysis. |
| `risk_quality` | DomainSpecificRubric — evaluates whether risk discussion mentions VaR, drawdown, Monte Carlo, and stress test results with specific values. |
| `signal_explanation_quality` | DomainSpecificRubric — scores whether the response explains three or more signal groups (risk, DCF, fundamentals, technicals, peer_positioning, behavioral) with specific numeric values. |
| `deterministic` | Zero-LLM schema validation via `score_quant_deterministic()` — checks all 8 signal groups present, weight sum=1.0, MC percentiles consistent, peer fields present, recommendation + confidence invariants. |

---



## Agent 4: Market Context (CrewAI)

| Property | Value |
|---|---|
| Framework | CrewAI |
| Executor | `GenericAgentExecutor(MarketContextAgent)` |
| LLM | LM Studio via CrewLLM |
| Data Collection | 3-step parallel pipeline via `asyncio.gather` |
| Port | 8004 |
| Agent Card | Built programmatically in `agent_4_crewai/server.py` |
| A2A Endpoint | `POST /a2a` |
| Health | `GET /health` → `{"status":"ok","agent":"market_context"}` |

### Skills

| ID | Name | Description |
|---|---|---|
| `macro_regime_analysis` | Macro Regime Analysis | Identifies the prevailing macro regime (yield curve, VIX, DXY, sector rotation) and explains whether it favors or penalizes the target ticker |
| `peer_landscape_analysis` | Peer Landscape Analysis | Compares the target ticker against 3-5 named peers on growth, margins, valuation, and recent price action — explains competitive positioning in narrative form |

### Architecture

```
Request → DefaultRequestHandler → GenericAgentExecutor(MarketContextAgent)
  → MarketContextAgent.stream(query, context_id, task_id)
    → try: yield await self._build_response(query)
    → finally: await self._disconnect()

MarketContextAgent._build_response(query):
    → extract_trace_ids(query)
    → with langfuse.start_as_current_observation(name="market-context-agent-stream")
      → MarketContextAgent.analyze(ticker)
        ├── _connect() → MCPClient.connect_all()
        ├── _collect_data_parallel(ticker)  (Phase 3 — macro + peers)
        │   ├── Step 1: get_macro_indicators() ∥ get_financials(ticker)
        │   │     → macro regime (yields, VIX, DXY, sector ETFs, yield curve)
        │   │     → primary financials (sector/industry for peer resolution)
        │   ├── Step 2: resolve peer tickers via shared/peer_sets.py
        │   └── Step 3: asyncio.gather(peer financials, peer prices)
        └── MarketContextCrew.analyze(ticker, precollected_data)
            └── Single Agent ("Market Context Analyst")
              → Outputs JSON: narrative, macro_regime, relative_peer_positioning,
                overall_signal (bullish/bearish/neutral), confidence_score (0-1),
                key_tailwinds, key_headwinds
      → if EVAL_ENABLED: asyncio.create_task(score_market_context_response(...))
      → return {response_type: "data", content: result, ...}
```

**Note**: The old Sentiment agent fetched `get_news_sentiment` and `get_company_filings` — both redundant with the RAG agent (Phase 1). As of Phase 3 (v1.31), Market Context fetches only macro indicators (15-min cached, no ticker argument) and peer financials/prices (shared `peer_sets.py` with Quant's `peer_comparison_node`). News and filings are exclusively the RAG agent's domain.

#### Runtime Evaluation

After each analysis, fires `asyncio.create_task(score_market_context_response(...))` with 3 metrics. Scored from `user_input`, `response`, and `_retrieved_contexts` (macro indicator values + peer financial summaries from pre-fetched data). `AnswerRelevancy` removed from Market Context (kept on Orchestrator only, v1.31).

| Metric | Why |
|---|---|
| `Faithfulness` | Verifies the narrative is factually grounded in the collected macro and peer data — a narrative that fabricates indicator values or peer metrics fails here. |
| `macro_regime_analysis` (DomainSpecificRubrics) | Custom 5-level rubric: scores whether the narrative discusses the yield curve (spread, regime), VIX level, DXY trend, and relevant sector ETF performance with actual values. |
| `peer_landscape_quality` (DomainSpecificRubrics) | Custom 5-level rubric: evaluates depth of peer comparison — whether named peers are contrasted on at least two metrics (PE, growth, margins, market cap) and competitive positioning is explained. |
