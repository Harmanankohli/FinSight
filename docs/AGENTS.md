# Agents Reference

## Agent 1: Orchestrator (ADK)

| Property | Value |
|---|---|
| Framework | Google ADK |
| Port | 8080 |
| Agent Card | Built programmatically in `src/orchestrator/main.py` |
| Discovery | `A2ACardResolver` via `/.well-known/agent-card.json`, async with 3x retry |
| A2A Endpoint | `POST /a2a` (via Starlette + `create_jsonrpc_routes`) |
| Health | `GET /health` ? `{"status":"ok","agent":"orchestrator"}` |

The orchestrator uses a single `LlmAgent` with two tools (`send_message`, `load_memory`). The `save_brief` function is still defined in `agent.py` but is no longer exposed as an LLM-callable tool — briefs are auto-saved via `after_agent_callback`. The LLM delegates tasks to sub-agents by name and synthesizes results:

1. **Discovers agents** via `SubAgentClient.discover()` — async `A2ACardResolver` with retries. In standalone mode (`main.py`), discovery runs as a lifespan background task. In ADK Web UI mode (`adk web`), discovery fires inside `_memory_cache_callback` on the first turn (the lifespan handler is never used under `adk web`). A `_discovery_done` module-level flag ensures it runs exactly once.
2. **Input guardrails** — Off-topic queries rejected in < 100 ms via `_NON_INVESTMENT_RE`. Invalid tickers rejected in < 2 s via pre-flight MCP `validate_ticker` call (using shared `resolve_and_validate_ticker()` from `ticker_utils.py`, same helper used by all sub-agents). Uses `get_shared_mcp()` singleton — no per-request connect/disconnect overhead.
3. **Semantic cache** — When `SEMANTIC_CACHE_ENABLED=true`, similar queries (cosine = 0.95) return cached responses without running the orchestrator. **Date-scoped**: `SemanticCache.set()` tags entries with today's `YYYY-MM-DD`; `SemanticCache.get()` uses a ChromaDB `where={"date": today}` filter matching only today's entries. Same query on a different day = cache miss = fresh analysis.
4. **Memory cache callback (before agent)** — `root_agent.before_agent_callback = _memory_cache_callback` fires before the LLM runs. Extracts the user's ticker, queries `TickerMemory.get_latest()`, and if today's brief exists with a valid `response_text`, returns it as `types.Content` — short-circuiting the LLM entirely. This is the fastest path for same-day repeat queries (~200ms vs 30-60s).
5. **Memory context injection** — If the cache callback misses (no today brief), extracts ticker from user input, retrieves latest recommendation from `TickerMemory`, and prepends it to the user message (~300 token budget). Tags as `[TODAY]` (fresh) or `[STALE]` (prior day) — LLM is instructed **"MUST return directly"** on `[TODAY]` rather than the previous "MAY return".
6. **LLM routes to agents in parallel** — The `_STATIC_PREAMBLE` explicitly instructs the model to emit ALL `send_message` calls in a SINGLE assistant response for parallel execution (`agent.py:184-189`). Qwen3-30B-A3B natively supports multiple tool calls in one turn. `_build_instruction()` also appends agent responsibility boundaries (`agent.py:253-260`) clarifying RAG owns ALL document/news retrieval, Market Context owns macro, Quant owns numeric analysis — preventing the LLM from routing news queries to the wrong agent. Each call measured with `time.monotonic()` and emitted as a Langfuse latency span. **`root_agent.instruction` is a callable** (`_instruction_provider`) that invokes `_build_instruction()` on every turn — newly discovered agents dynamically appear in the system prompt without a process restart.
7. **Phase 2 — Review** — After all Phase 1 responses, call `send_message` for the Reviewer Agent. `send_message` now uses `SendMessageInput(agent_name, ticker)` Pydantic model instead of free-text `task: str`. Task content is code-built from `_AGENT_TASK_TEMPLATES` dict. Reviewer receives richer condensed summaries with per-agent metrics (beta, VaR, DCF, ROE, RSI, golden cross, MC p50, macro_regime, anomaly_count) instead of raw full agent outputs. Pre-reviewer `validate_metric_integrity()` checks mathematical invariants before synthesis.
8. **Output guardrails** — Responses shorter than 50 chars trigger `TASK_STATE_FAILED`. Missing BUY/HOLD/SELL signal emits a Langfuse warning with `missing_signal: true`.
9. **Synthesizes results** — LLM collects all outputs and produces a BUY/HOLD/SELL recommendation
10. **Auto-save** — After each response, persists ticker brief, portfolio holdings, and performance record (with live price snapshot via yfinance) to SQLite. Fires background task to evaluate past recommendations. Briefs are auto-saved via `after_agent_callback` — the LLM does not need to call `save_brief`. Confidence scores are extracted from response text via regex matching "Confidence Score: X", "confidence: X", and "X% confidence" formats. Agent output capture (`send_message` callback) stores parsed sub-agent responses (RAG summary, quant metrics, sentiment narrative) in session event metadata via `extra_data` on `store_minimal()`. ADK Web UI path now also captures agent responses via `_collect_agent_extra()` in `after_agent_callback` — previously structured agent data was silently lost on web UI queries.
11. **Memory persist + Runtime RAGAS evaluation** — All run from `after_agent_callback` in `src/orchestrator/web/agent.py`. The callback first checks `_is_analysis_turn()` (was `save_brief` called in this turn or were `send_message` invocations detected?) — non-analysis turns like "what were my last recommendations?" skip persist + eval to avoid memory pollution. Analysis turns then call `add_events_to_memory()` and fire `asyncio.create_task(_eval_score_response(...))` with 6 metrics. The callback also fires `_release_sub_agent_evals()` — a fire-and-forget task that POSTs to each sub-agent's `/release-evals` endpoint so their deferred evals run. The full synthesis text is stored directly by `save_brief` (reads longest LLM text from session events) — the post-turn `update_response_text` overwrite was removed. All gated globally by `EVAL_TRACE_ENABLED`. Scores pushed to Langfuse under `ragas/orchestrator/<metric>`.
12. **Eval hardening** — The `_gate_ok()` pre-eval gate enforces: **circuit breaker** (5 consecutive metric failures opens the circuit, skipping eval for the rest of the process lifetime), **SHA-256 dedup** (identical input+response pairs skip eval via 1h TTL dict), **burst limiter** (`_burst_ok()` enforces `EVAL_BURST_LIMIT` evaluations per minute), and **per-metric timeout** (`_score_metric_with_timeout()` default 90s via `EVAL_METRIC_TIMEOUT`).
13. **Memory pruning** — `prune_old_records()` called on startup deletes rows older than `MEMORY_RETENTION_DAYS=90` from `ticker_briefs`, `recommendation_records`, and `memory_entries`. Uses existing `write_lock()` for concurrency safety. Returns dict of `{table: rows_deleted}`.
14. **Cancellation support** — `FinSightAgentExecutor.cancel()` stores `asyncio.current_task()`, catches `CancelledError`, emits `TASK_STATE_CANCELED`.
15. **IST timezone** — All datetime comparisons use `IST = timezone(timedelta(hours=5, minutes=30))`. Relevant for "today" comparisons in memory cache callback and auto-save timestamping.
16. **Per-agent timeouts** — `SubAgentClient.send_message()` wraps streaming in `asyncio.wait_for()` with per-agent timeouts from `shared.settings`: RAG=600s, Quant=600s, Market Context=600s, fallback = global `A2A_TIMEOUT=680s`. Timeout returns clean `{"error": "agent_timeout", ...}` JSON to the LLM. AG-UI bridge uses `asyncio.wait` instead of `wait_for` to prevent `CancelledError` propagating into ADK runner. LLM timeout raised to 600s.

All A2A communication uses `ClientFactory` + `BaseClient` from the official `a2a-sdk`. Streaming events are handled correctly: intermediate SUBMITTED/WORKING events are skipped, only `artifact_update` events (data or text) and terminal `status_update` events are returned to the LLM.

### Architecture

```
Module load (standalone — src/orchestrator/main.py):
  ? SubAgentClient.discover() in lifespan background task
    +-- A2ACardResolver(http, "http://localhost:8002")
    +-- A2ACardResolver(http, "http://localhost:8003")
    +-- A2ACardResolver(http, "http://localhost:8004")
    ? self.agents populated ? instruction updated (callable _instruction_provider)

ADK Web UI (services.py at src/orchestrator/):
  ? Discovery fires inside _memory_cache_callback on first turn
    — main.py lifespan never used under adk web
    — root_agent.instruction is callable: builds instruction per-turn

FinSightAgentExecutor:
  A2A Request ? execute()
  ? _build_memory_context(query) ? inject [MEMORY CONTEXT] prefix
  ? RUNNER.run_async(user_query)
  ? ADK LlmAgent (tools: [send_message, load_memory])
    ? LLM emits ALL send_message calls in ONE assistant turn (parallel, per system prompt instruction)
    ? send_message(SendMessageInput(agent_name="Financial RAG Agent", ticker="NVDA"))
    ? send_message(SendMessageInput(agent_name="Quant Analysis Agent", ticker="NVDA"))
    ? send_message(SendMessageInput(agent_name="Market Context Agent", ticker="NVDA"))
    ? LLM receives all results together in the next turn
    ? LLM synthesizes BUY/HOLD/SELL
    ? load_memory(query="...") — search past conversations
  ? _add_events_to_memory() ? get_session() ? add_session_to_memory()
  ? _persist_to_memory() ? direct events ? SQLiteMemoryService (for load_memory)
  ? _store_memory() ? TickerMemory + PortfolioStore + PerformanceTracker
  — send_message uses SendMessageInput(agent_name, ticker) Pydantic model
  — Task templates built from _AGENT_TASK_TEMPLATES dict (no free-text LLM task construction)

before_agent_callback (ADK Web UI path — fires before LLM every turn):
  ? _memory_cache_callback(callback_context)
       ? Extract user ticker from session.events (regex)
       ? TickerMemory.get_latest(ticker, user_id=None)
         +-- hit ? return types.Content directly (short-circuit)
         +-- miss ? fall back to MCP resolve_company_ticker for company-name tokens
         —          (e.g. "VISA" ? canonical "V") then retry cache lookup
         +-- still miss ? return None (let LLM run)

Executor-level cache (A2A path — orchestrator/agent_executor.py):
  A2A Request ? execute()
  ? _get_today_cached_text(ticker) — same check, returns cached text directly
  ? [miss] ? _build_memory_context() ? RUNNER.run_async()

after_agent_callback (ADK web UI path — primary path; run_adk_web.bat no longer starts orchestrator/main.py; services.py at src/orchestrator/services.py because ADK rewrites agents_dir to parent when it detects web/agent.py):
  ? _is_analysis_turn(session.events) — was save_brief called OR was send_message invoked?
       +-- No  ? skip persist + eval (memory recall turn, e.g. load_memory only)
       +-- Yes ?
             ? _collect_agent_extra() — pop agent responses from send_message events, map into brief_json keys
             ? callback_context.add_events_to_memory(events=session.events, custom_metadata={...})
             ? SQLiteMemoryService.add_events_to_memory() ? memory_entries table
             ? if EVAL_ENABLED: asyncio.create_task(score_response(query, response, trace_id))
                  ? ragas/orchestrator/{AnswerRelevancy, citation_quality, risk_disclosure,
                                        recommendation_clarity, response_completeness}
             ? asyncio.create_task(_release_sub_agent_evals())  — POST /release-evals to all sub-agents
```

### Streaming Event Flow

When `send_message` is called, sub-agents respond with streaming events:

```
event 1: task { state: SUBMITTED }          ? skipped (non-terminal)
event 2: status_update { WORKING, msg }     ? skipped (non-terminal)
event 3: artifact_update { data: {...} }    ? returned (actual result)
    OR status_update { COMPLETED, msg }     ? returned (terminal text)
    OR task { state: COMPLETED, artifacts } ? returned (non-streaming fallback)
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

Triggered from `after_agent_callback` (ADK Web path) — see step 10 above. Fires `asyncio.create_task(score_response(...))` with 6 metrics (all scored from `user_input` + `response` only — no ground-truth reference needed). Also fires `_release_sub_agent_evals()` as a background task, which POSTs to each sub-agent's `/release-evals` endpoint to fire their deferred eval coroutines. Globally toggled by `EVAL_TRACE_ENABLED` in `.env`. All LLM-based metrics use `LOW` priority in `LLMPriorityQueue` — they yield to production `CRITICAL` calls when the LM Studio instance is saturated. Scores pushed to Langfuse under `ragas/orchestrator/<metric>`.

| Metric | Why |
|---|---|
| `AnswerRelevancy` | Measures whether the final BUY/HOLD/SELL recommendation addresses what the user asked. Generic catch-all. |
| `citation_quality` (DomainSpecificRubrics) | Custom 5-level rubric: scores whether the response cites specific filing dates, sections, and monetary figures vs making generic claims. A response saying "NVDA's revenue grew" without citing the 10-Q date and amount scores 1. Critical for financial credibility — unsubstantiated claims are worthless. |
| `risk_disclosure` (DomainSpecificRubrics) | Custom 5-level rubric: evaluates whether risks are acknowledged. An investment thesis without risk discussion is incomplete. Scores from "no risk mentioned" (level 1) to "balanced multi-category risk assessment" (level 5). |
| `recommendation_clarity` (DomainSpecificRubrics) | Custom 5-level rubric: verifies the synthesizer produces an explicit BUY/HOLD/SELL signal with supporting evidence from =2 sub-agents. A response that discusses pros/cons without committing to a clear signal scores low. |
| `response_completeness` (DomainSpecificRubrics) | Custom 5-level rubric: assesses whether all three analysis types (SEC filings, quant metrics, market context) are synthesized. A response that only discusses stock price without filing data or narrative scores 1. |
| `no_forward_guarantees` (AspectCritic) | Flags any language suggesting guaranteed future performance. Prevents the orchestrator from making forward-looking promises. |

---

## Agent 2: RAG (LlamaIndex)

| Property | Value |
|---|---|
| Framework | LlamaIndex |
| Executor | `GenericAgentExecutor(RAGAgent)` in `src/financial_rag/executor.py` |
| LLM | LM Studio via `llama-index-llms-openai-like` (API key from `LLM_API_KEY` env var) |
| Vector Store | ChromaDB (local, persisted to `./db/chroma_db`) |
| Embeddings | HuggingFace `all-MiniLM-L6-v2` (local) |
| Port | 8002 |
| Agent Card | Built programmatically in `src/financial_rag/server.py` |
| A2A Endpoint | `POST /a2a` |
| Health | `GET /health` ? `{"status":"ok","agent":"rag"}` |

### Skills

| ID | Name | Description |
|---|---|---|
| `sec_filing_retrieval` | SEC Filing Retrieval | Retrieves and analyzes SEC 10-K, 10-Q, 8-K filings |
| `earnings_summary` | Earnings Summary | Summarizes earnings call transcripts |
| `financial_news_retrieval` | Financial News Retrieval | Retrieves and analyzes recent financial news articles with sentiment context for impact assessment on stock prices |

### Startup Warm-up

On Starlette startup, `_prewarm()` runs `_do_prewarm()` in a thread executor to pre-load:
- HuggingFace embedder + dummy encode (~1s)
- 3 ChromaDB collections (`sec_filings`, `news`, `earnings`)
- CrossEncoder reranker (~2s)

Per-stage timing logged. Effect: first RAG query no longer pays ~3-5s model-load tax.

### Architecture

```
Request ? DefaultRequestHandler ? GenericAgentExecutor(RAGAgent)
  ? RAGAgent.stream(query, context_id, task_id)
    ? await self._build_response(query)
    ? return {response_type: "data", content: result, ...}

RAGAgent._build_response(query):
    ? extract_trace_ids(query)
    ? with langfuse.start_as_current_observation(name="rag-agent-stream")
      ? ticker = extract_ticker(query)  — supports dotted (BRK.A), single-char (V), $prefix
      ? Fallback chain: regex ? MCP resolve ? MCP validate (via get_shared_mcp())
      ? asyncio.create_task(self._ensure_ingested(ticker))      (Phase 2 — fire-and-forget)
      ? asyncio.create_task(self._ensure_news_ingested(ticker))
      ? FinancialIndexManager.query(ticker, query)  — returns immediately from indexed data
        +-- _classify_query_intent() ? sec_filings ? news ? earnings
        +-- Multi-collection retrieval with hybrid scoring (dense + keyword + temporal)
        +-- LlamaIndex response synthesizer (response_mode="compact", similarity_top_k=3)
            — single LLM call per query, not N sequential refine calls
      ? No intermediate WORKING yield — follows single-yield pattern (one data response on completion)
      ? if EVAL_ENABLED: defer_eval(score_rag_response, ...) (deferred via shared/eval_gate.py — released by orchestrator's POST /release-evals)
      ? return {response_type: "data", content: result, ...}

RAGAgent._ensure_ingested(ticker) [background]:
  ? mcp = await get_shared_mcp()
  ? MCP: get_financial_filings(ticker, annual_limit=3, quarterly_limit=4) ? {annual[], quarterly[]} with edgar_url + ix_url
  ? Filter: is_filing_ingested(edgar_url) — skip already-indexed URLs
  ? Parallel fetch via asyncio.gather:
    +-- MCP: get_filing_content(edgar_url, ix_url) for candidate 1
    +-- MCP: get_filing_content(edgar_url, ix_url) for candidate 2
    +-- ... (all candidates concurrently, truncated server-side at 25k chars)
  ? DocumentIngestionPipeline.ingest_sec_filings_batch() ? ChromaDB
  ? mark_filing_ingested(edgar_url, ticker) for each new filing

News ingestion (separate from SEC filings):
  ? Fetches get_news_sentiment (15 articles)
  ? Daily dedup key: "news_{ticker}"
  ? Independent of SEC filing ingestion — both run concurrently
```

#### Runtime Evaluation

After each response, fires `asyncio.create_task(score_rag_response(...))` with 4 metrics. Scored from `user_input`, `response`, and `context_texts` (ChromaDB source chunks). No ground-truth reference required.

| Metric | Why |
|---|---|
| `Faithfulness` | Verifies every claim in the response is directly supported by the retrieved source text. Prevents hallucinated dates, numbers, or citations. |
| `AnswerRelevancy` | (Removed — kept on Orchestrator only) |
| `ContextPrecisionWithoutReference` | Evaluates whether the retrieved ChromaDB chunks are relevant to the query. Flags retrieval drift — when RAG returns irrelevant filings, this drops even if Faithfulness passes. |
| `cross_collection_synthesis` (DomainSpecificRubrics) | Checks whether the response cites sources from =2 collections (sec_filings, news, earnings). A response drawing only from a single collection scores low. |

---

## Agent 3: Quant (LangGraph)

| Property | Value |
|---|---|
| Framework | LangChain + LangGraph |
| Executor | `GenericAgentExecutor(QuantAgent)` in `src/quant/executor.py` |
| LLM | LM Studio via `langchain-openai` (summary node only; API key from `LLM_API_KEY` env var) |
| LLM Cache | LangChain `SQLiteCache` — identical inputs reuse cached LLM response |
| Data Source | MCP (finsight-mcp `get_prices`, `get_financials`, `get_options_chain`, `get_company_filings`, `get_peers`, `get_scenario_shocks`, `get_insider_transactions`) via `get_shared_mcp()` |
| Port | 8003 |
| Agent Card | Built programmatically in `src/quant/server.py` |
| A2A Endpoint | `POST /a2a` |
| Health | `GET /health` ? `{"status":"ok","agent":"quant"}` |

### Skills

| ID | Name | Description |
|---|---|---|
| `quant_analysis` | Quantitative Analysis | Compute Sharpe ratio, Beta, VaR, volatility, DCF valuation, stress tests, Monte Carlo |
| `options_flow_analysis` | Options Flow Analysis | Analyze put/call volume and open interest ratios to detect unusual options activity |
| `insider_transaction_analysis` | Insider Transaction Analysis | Track Form 4 filings, detect cluster buying/selling patterns by executives |
| `positioning_signals` | Positioning Signals | Evaluate short interest, analyst consensus, earnings surprise history, and squeeze risk |

### Architecture

```
Request ? DefaultRequestHandler ? GenericAgentExecutor(QuantAgent)
  ? QuantAgent.stream(query, context_id, task_id)
    ? yield await self._build_response(query)
    ? return {response_type: "data", content: result, ...}

QuantAgent._build_response(query):
    ? extract_trace_ids(query)
    ? with langfuse.start_as_current_observation(name="quant-agent-stream")
      ? extract_holdings(query, exclude_ticker=ticker) ? ["AAPL", "MSFT", "GOOGL"]
      ? QuantAgent.analyze(ticker, portfolio_holdings=holdings)
        [Parallel fan-out from START — Phase 4 adds 3 behavioral nodes]
          +-- fetch_prices ? compute_metrics ? technical_analysis
          —     (SMA, MACD, RSI, Bollinger, support/resistance, trend)
          —     ? volatility gate ? stress_test (beta-adjusted) XOR dcf_valuation
          —         (data-driven WACC via CAPM + CoD, tapered growth)
          —         Stress test: beta_adj_decline = mkt_decline * beta, floored at -95%
          —         Live sector-aware shocks via MCP get_scenario_shocks (QQQ/XLP/XLF...)
          —     ? monte_carlo (GBM, 5,000 paths, 252-day horizon) — runs in BOTH paths
          —         (stress_test_node for high-vol, dcf_valuation_node for low-vol)
          +-- fetch_fundamentals ? 25+ ratios (PE, ROE, margins, D/E, etc.)
          —     + derived signals (golden cross, net debt, 52w extremes)
          —     ? peer_comparison (dynamic via MCP get_peers — yfinance Industry/Sector classes,
          —         ranks on PE, EV/EBITDA, growth, margins, ROE, D/E)
          —         ? sector medians computed for relative scoring of fundamentals
          +-- options_flow_node (put/call vol ratio, OI ratio, flow signal, no-data handling)
          +-- insider_signals_node (get_insider_transactions MCP — structured buy/sell data, not Form 4 keyword parsing)
          +-- analyst_positioning_node (consensus, upside %, short interest, squeeze)
        ? portfolio_correlation
        ? format_output (8-group weighted voting, sum=1.0)
          (risk_quality 0.15, dcf_value 0.20, fundamental_value 0.13, fundamental_quality 0.12,
           technicals_trend 0.15, technicals_momentum 0.10, peer_positioning 0.10, behavioral 0.05)
        ? llm_summary (data-readiness guard — skips if predecessors incomplete)
               (CRITICAL priority queue — enriched 3-4 sentence summary)
      ? if EVAL_ENABLED: defer_eval(score_quant_response, ...) (deferred via shared/eval_gate.py)
      ? return {response_type: "data", content: result, ...}
```

### Startup Warm-up

On Starlette startup, `_prewarm()` runs an LLM ping via LangChain's `aiohttp` session to pre-warm the model server connection. Uses `LLMPriorityQueue` with `NORMAL` priority — ensures the warmup ping doesn't block production inference if startup coincides with concurrent eval scoring.

**Portfolio Holdings Extraction**: `extract_holdings()` in `src/shared/ticker_utils.py` uses regex patterns to extract holdings from natural language (e.g. "My portfolio holds AAPL, MSFT, GOOGL"). The orchestrator LLM is instructed to include holdings in the task text for the Quant agent. Holdings are passed through the full chain and used by `correlation_node` to compute a correlation matrix.

**Correlation Matrix Notes**: When no holdings are provided, returns `{"note": "No portfolio holdings provided..."}` instead of `{}`.

**Logging & Error Propagation**: The graph logs routing decisions, metric computation failures, DCF fallbacks, and beta calculation errors. When DCF is skipped due to high volatility, the `dcf_error` field is set and propagated through formatting and LLM summary, giving users visibility into why DCF was not computed.

**Monte Carlo Simulation**: `_run_monte_carlo()` at `nodes.py:42` runs 5,000 geometric Brownian motion paths over a 252-day horizon. Returns p10/p25/p50/p75/p90 percentiles, probability of profit, and MC VaR(95). Stored in `QuantAnalysisState.monte_carlo`.

**8-Group Weighted Voting**: `_SIGNAL_WEIGHTS` at `nodes.py:108-124` sum to 1.0 across 8 groups. Confidence = `|composite| — (1 - std(present_signals))`. Raw weighted sum (not normalized) — signal density naturally reduces composite when fewer signals are present. Previous normalized-weight approach distorted confidence when few signals were present.

**Peer Discovery**: The `peer_comparison_node` uses dynamic peer discovery via MCP `get_peers` (yfinance `Industry`/`Sector` classes), not `src/shared/peer_sets.py`. The static `peer_sets.py` is available as a fallback. The Market Context agent also uses dynamic `get_peers`.

#### Runtime Evaluation

After each analysis, fires `asyncio.create_task(score_quant_response(...))` with 2 RAGAS metrics + 1 deterministic schema validator. Scored from `user_input`, `response`, and full `quant_result` dict (Sharpe, VaR, DCF, beta, MC, signals) serialized as a reference string. `AnswerRelevancy` removed from Quant (kept on Orchestrator only, v1.31). RAGAS metric LLM calls use `LOW` priority in `LLMPriorityQueue` — they yield to production `CRITICAL` calls (Quant `llm_summary_node`).

| Metric | Why |
|---|---|
| `FactualCorrectness` | Compares the LLM summary's numerical claims against the actual computed metrics. The reference is the raw quant result dict — if the LLM says "Sharpe ratio of 1.5" but the computation produced 0.8, this metric catches it. Prevents hallucinated numbers, the primary failure mode for quantitative analysis. |
| `signal_explanation_quality` (DomainSpecificRubrics) | Scores whether the response explains three or more signal groups (risk, DCF, fundamentals, technicals, peer_positioning, behavioral) with specific numeric values. |
| `deterministic` (schema validator) | Zero-LLM schema validation via `score_quant_deterministic()` — checks all 8 signal groups present, weight sum=1.0, MC percentiles consistent, peer fields present, recommendation + confidence invariants. Runs inline (not a RAGAS metric). |

---

## Agent 4: Market Context (CrewAI)

| Property | Value |
|---|---|
| Framework | CrewAI |
| Executor | `GenericAgentExecutor(MarketContextAgent)` in `src/market_context/executor.py` |
| LLM | LM Studio via `CrewLLM` (API key from `LLM_API_KEY` env var) |
| Data Collection | 3-step parallel pipeline via `asyncio.gather` |
| Port | 8004 |
| Agent Card | Built programmatically in `src/market_context/server.py` |
| A2A Endpoint | `POST /a2a` |
| Health | `GET /health` ? `{"status":"ok","agent":"market_context"}` |

### Skills

| ID | Name | Description |
|---|---|---|
| `macro_regime_analysis` | Macro Regime Analysis | Identifies the prevailing macro regime (yield curve, VIX, DXY, sector rotation) and explains whether it favors or penalizes the target ticker |
| `peer_landscape_analysis` | Peer Landscape Analysis | Compares the target ticker against 3-5 named peers on growth, margins, valuation, and recent price action — explains competitive positioning in narrative form |

### Architecture

```
Request ? DefaultRequestHandler ? GenericAgentExecutor(MarketContextAgent)
  ? MarketContextAgent.stream(query, context_id, task_id)
    ? yield await self._build_response(query)
    ? return {response_type: "data", content: result, ...}

MarketContextAgent._build_response(query):
    ? extract_trace_ids(query)
    ? with langfuse.start_as_current_observation(name="market-context-agent-stream")
      ? MarketContextAgent.analyze(ticker)
        +-- mcp = await get_shared_mcp()
        +-- _collect_data_parallel(ticker)  (Phase 3 — macro + peers)
        —   +-- Step 1: asyncio.gather(get_macro_indicators(), get_financials(ticker))
        —   —     ? macro regime (yields, VIX, DXY, sector ETFs, yield curve)
        —   —     ? primary financials (sector/industry for peer resolution)
        —   +-- Step 2: resolve peers via MCP get_peers (dynamic, Yahoo Finance API)
        —   +-- Step 3: asyncio.gather(peer financials, peer prices for each peer)
        +-- MarketContextCrew.analyze(ticker, precollected_data)  (CRITICAL priority queue — crew.kickoff() issues LLM call)
            +-- Single Agent ("Market Context Analyst") — no crew collaboration, data is pre-collected
              ? Outputs MarketContextOutput (Pydantic model, v2.5): narrative, macro_regime,
                overall_signal (bullish/bearish/neutral), confidence_score (0-1),
                key_tailwinds, key_headwinds
      ? if EVAL_ENABLED: defer_eval(score_market_context_response, ...) (deferred via shared/eval_gate.py)
      ? return {response_type: "data", content: result, ...}
```

**Note**: The old Sentiment agent fetched `get_news_sentiment` and `get_company_filings` — both redundant with the RAG agent (Phase 1). As of Phase 3 (v1.31), Market Context fetches only macro indicators (15-min cached, no ticker argument) and peer financials/prices via dynamic MCP `get_peers`. News and filings are exclusively the RAG agent's domain. Peer sets use live Yahoo Finance API discovery, not `src/shared/peer_sets.py` (which exists as a fallback).

**Note**: The Market Context agent is a simple single-agent setup — not a multi-agent CrewAI crew. Data is pre-collected in `_collect_data_parallel()` and injected directly into the task context. The single CrewAI Agent produces a JSON narrative from the pre-collected data.

**Note**: As of v1.34, `_collect_data_parallel()` correctly extracts `sector` and `industry` from `get_financials` response before passing to the crew context.

#### Runtime Evaluation

After each analysis, fires `asyncio.create_task(score_market_context_response(...))` with 3 metrics. Scored from `user_input`, `response`, and `_retrieved_contexts` (macro indicator values + peer financial summaries from pre-fetched data). `AnswerRelevancy` removed from Market Context (kept on Orchestrator only, v1.31). RAGAS metric LLM calls use `LOW` priority in `LLMPriorityQueue` — they yield to production `CRITICAL` calls (`crew.kickoff()`).

| Metric | Why |
|---|---|
| `Faithfulness` | Verifies the narrative is factually grounded in the collected macro and peer data — a narrative that fabricates indicator values or peer metrics fails here. |
| `macro_regime_analysis` (DomainSpecificRubrics) | Custom 5-level rubric: scores whether the narrative discusses the yield curve (spread, regime), VIX level, DXY trend, and relevant sector ETF performance with actual values. |
| `peer_landscape_quality` (DomainSpecificRubrics) | Custom 5-level rubric: evaluates depth of peer comparison — whether named peers are contrasted on at least two metrics (PE, growth, margins, market cap) and competitive positioning is explained. |

## Shared Infrastructure

All four agents share a common infrastructure layer:

| Component | Details |
|---|---|
| **MCP client singleton** | `get_shared_mcp()` returns a process-wide `MCPClient` instance. Auto-reconnects on `ConnectionError`/`EOFError`/`IncompleteReadError`. No per-request connect/disconnect. |
| **Ticker validation** | `resolve_and_validate_ticker()`, `validate_ticker()`, and `resolve_ticker()` in `src/shared/ticker_utils.py` use `get_shared_mcp()` internally. `resolve_and_validate_ticker()` is a unified helper combining both steps — shared across all 5 agents (RAG, Quant, Market Context, Analytics, Reviewer), replacing per-agent private methods. Blocks financial acronyms (SEC, EPS, CEO, etc.) via hardcoded `_COMMON_ACRONYMS` set. |
| **`@logged` timing decorator** | Applied to all three sub-agent `_build_response()` methods and `SubAgentClient.send_message()`. Logs elapsed time and entry/exit. |
| **Lazy OpenTelemetry** | `init_instrumentation("<agent_type>")` replaces module-level `*Instrumentor().instrument()` calls. Called from each server entry point. |
| **SQLiteTaskStore** | Replaces `InMemoryTaskStore` in all four server entry points. Tasks survive process restarts. |
| **`LLM_API_KEY` env var** | Replaces hardcoded `api_key="lmstudio"`. All agent files import from `shared.settings` (was `src/shared/config.py`, removed in v2.0). |
| **Centralized settings** | `src/shared/settings.py` (pydantic-settings `BaseSettings`) with back-compat aliases, `validate_runtime()`, `get_settings()` singleton. `src/shared/bootstrap.py` centralises process-level side-effects (event loop policy, `HF_HUB_OFFLINE`, stdout encoding). |
| **Per-service log levels** | `LOG_LEVEL_<SERVICE>` env vars (e.g. `LOG_LEVEL_ORCHESTRATOR=DEBUG`). |
| **`SEC_USER_AGENT` env var** | Replaces hardcoded SEC user agent string in MCP server filing requests. |
| **LLM Priority Queue** | `LLMPriorityQueue` in `src/shared/llm_queue.py` (process-local, heap-based async semaphore). Three tiers: `CRITICAL` (quant summary, crew kickoff), `NORMAL` (warmup ping), `LOW` (RAGAS eval). Default `LLM_MAX_CONCURRENT=2`. Prevents eval starvation of production inference. |
| **Deferred Eval Gate** | `src/shared/eval_gate.py` — holds sub-agent eval LLM calls until the orchestrator releases them via `POST /release-evals` after synthesis completes. Prevents 3 sub-agent eval processes from competing with orchestrator synthesis on a single LM Studio instance. Includes 120s safety-net auto-release. |
| **Pydantic Agent Output Models** | `src/shared/agent_models.py` (v2.5) — typed models (`QuantAgentOutput`, `MarketContextOutput`, `RAGAgentOutput`) at every agent boundary, replacing ~220 lines of fragile regex/`.get()` chains. Fixes zeroed KPI chips, raw JSON narrative from CrewAI, and DCF key mismatch. Falls back to legacy dict extraction for old briefs. |
| **`SERVICE_AUTH_TOKEN`** | `SubAgentClient` accepts `bearer_token` from `settings.service_auth_token`. When `AUTH_ENABLED=true`, orchestrator-to-sub-agent A2A requests carry the service bearer token. |
| **Shared Agent Output Store** | `src/shared/memory/agent_output_store.py` (v2.7) — SQLite table `agent_output_store` keyed by `(session_id, agent_name)`. Full agent outputs persisted by orchestrator `send_message` callback, fetched by reviewer executor via `get_agent_outputs()`. TTL-pruned at startup. |
| **Shared Metrics (MetricValue)** | `src/shared/metrics.py` (v2.9) — `MetricValue` float subclass with validation metadata (status, warning, methodology, to_dict). Used by Quant and Analytics agents for all metric computations. Re-exports from `shared.__init__`. |

## Phase Map

The project evolved through thirteen phases, each adding distinct agent capabilities:

| Phase | Version | What Changed |
|---|---|---|
| **Phase 1** | v1.29 | RAG news/earnings ingestion, Quant fundamentals/technicals/DCF |
| **Phase 2** | v1.30 | Parallel dispatch to sub-agents, parallel filing downloads, single-flight ingestion dedup |
| **Phase 3** | v1.31 | Sentiment Agent ? Market Context Agent rebrand. Quant behavioral signals (options, insider, positioning). RAGAS runtime eval for all 4 agents. Eval circuit breaker, dedup, burst limiter. Date-scoped memory persistence gate (`_is_analysis_turn`). |
| **Phase 4** | v1.31-1.32 | Date-scoped semantic cache. RAG startup warm-up. `no_forward_guarantees` AspectCritic. Stress test beta-adjusted formula. 8-group weighted voting normalization fix. |
| **Phase 5** | v1.33-1.35 | Quant graph fan-in reducer fixes (concurrent update, diamond dependency, duplicate fan-in). Dynamic peer discovery via yfinance Industry/Sector classes. Live sector-aware scenario shocks with sector ETF benchmarks. Sector-relative fundamental scoring. Structured `get_insider_transactions` MCP tool replacing Form 4 text parsing. `get_peers` MCP tool using yfinance. Expanded `peer_sets.py` with normalised key matching. Monte Carlo runs on both high-vol and low-vol paths. Options flow zero-volume edge case handling. Null-safe schema validator for quant deterministic eval. yfinance blocking calls moved to thread executor (`run_in_executor`). Peer concurrency capped at 3 (`asyncio.Semaphore`). Redis auto-start in `run_adk_web.bat`. MCP client timeout simplification (removed fail-fast first-attempt timeout). All 9 sync yfinance calls now wrapped in `run_in_executor` (7 more added: prices, financials, macro, options chain, earnings calendar, sentiment indicators, earnings history). `httpx.ReadError`/`ConnectError`/`NetworkError` added to MCP client transient retry set. RAGAS eval retry tuning: `max_retries=5`, separate `asyncio.TimeoutError` handling, empty exception message classification. |
| **Phase 6** | v1.37 | LLM Priority Queue (`src/shared/llm_queue.py`) — 3-tier heap-based async semaphore to prevent RAGAS eval starvation of production LLM inference. Quant `llm_summary_node` and CrewAI `crew.kickoff()` use `CRITICAL` priority; server warmup uses `NORMAL`; all runtime eval metrics use `LOW` priority. Controlled by `LLM_MAX_CONCURRENT` env var (default 2). |
| **Phase 7** | v1.38 | Deferred Eval Gate (`src/shared/eval_gate.py`) — cross-process eval coordination. Sub-agents defer evals via `defer_eval()` instead of `asyncio.create_task()`; orchestrator POSTs `/release-evals` after synthesis. Confidence regex updated for "Confidence Score: X" format. AG-UI bridge auto-saves briefs and strips null optional fields recursively for CopilotKit Zod compatibility. |
| **Phase 8** | v1.39 | Report generator overhaul: `_resolve_ticker_info()` via yfinance replaces hardcoded 28-symbol dict; `_parse_markdown_tables()` captures structured LLM table data before markdown cleanup (fixes empty Financial Performance slides); Momentum/RSI added as 6th scorecard dimension; `generate_html()` added as public API using Jinja2 templates (`src/shared/templates/`), wired into routes and agent tool dispatch; 9 PPTX slide generators extracted into standalone functions; AG-UI bridge serialization fix for `LoadMemoryResponse`; report route ordering fixed so `/ticker/{symbol}/latest/{format}` matches before generic `/{brief_id}/{format}` catch-all; Python 3.12 lookbehind crash fixed; 30 new regression + unit tests. |
| **Phase 9** | v1.40 | Report extraction hardening: section parser extended to `###`/`####` headers; Bear Case target, bare ticker peers (DLTR/COST), Bearish/Bullish Signals inline blocks, Tailwinds/Headwinds labels added to extraction pipeline; structured peer comparison from Quant + Sentiment agents; Monte Carlo p10/p50/p90 scenario cards. Agent: custom `load_memory` wrapper returns plain `str` (fixes serialization crash); `send_message`-first instruction hardened; `generate_report` removed from LLM tool list. Circular import in `src/shared/trace_context.py` fixed. Logging overhaul: 11 silent excepts fixed, 7 files got logger, operational log statements, noisy third-party loggers suppressed, `@logged` on `GenericAgentExecutor.execute()`. Diagram Mermaid syntax + zoom/drag fixed. |
| **Phase 10** | v1.41 | Centralized settings (`src/shared/settings.py` pydantic-settings), module splits (MCP server ? `tools/` + `infra/`, report_generator ? `src/shared/reports/`, nodes.py ? `nodes/`), guardrails unification, Docker hardening (non-root USER, per-service extras, healthchecks), `build_agent_app()` factory. |
| **Phase R** | v1.42 | Table classification, bounded bull/bear extraction, staged extraction pipeline, corpus regression harness (7 fixtures, invariant tests), `export_brief_fixtures.py` script. |
| **Phase 11** | v1.43 | Full auth implementation — JWT tokens, Argon2 passwords, refresh rotation, AuthMiddleware with principal-kind routing, user store, rate-limited lockout, A2A service auth, MCP SSE auth, sandbox container mode (`SANDBOX_MODE=container`), Caddyfile.example, 42 auth unit tests. |
| **Phase 12** | v2.0 | Contract tests (auth matrix, A2A protocol), OpenAPI spec (13 paths, 16 schemas, CI-enforced), trace filter (`traceFilter.ts`, `trace_with_user()`), shim removal (`src/shared/config.py`/`report_generator.py` deleted, ruff/mypy clean). |
| **—** | v2.1 | Post-Phase-3 runtime fixes (6 error resolutions), CI hardening (ruff cleanup, uv venv, slim test deps, lazy memory imports, 22 test fixes, coverage/pytest hang fixes), proxy.ts disabled. |
| **—** | v2.2 | Agent output capture (structured sub-agent responses stored via `extra_data` on `store_minimal()`), Playwright-based HTML→PPTX/PDF export (`src/shared/reports/playwright_export.py`), `SentimentIntelligence` → `MarketContext` model rename, ticker extraction hardening (pronouns + holdings word boundaries), stable anonymous user ID cookie, AG-UI bridge per-event timeout, 16 new agent-output extraction tests. |
| **—** | v2.3 | Source tree restructure to `src/` layout. Model rename + field restructure (key_risks→key_headwinds, etc.). |
| **—** | v2.4 | ADK Web UI discovery fix (before_agent_callback), callable instruction, services.py moved. RAG `get_financial_filings` switch. Auth public endpoints, frontend auth middleware/redirect, service token forwarding. |
| **—** | v2.5 | Pydantic output models for all agents (`src/shared/agent_models.py`), scrollable HTML report + A4 PDF (replaced slide deck), AG-UI bridge eval hook + sentence-aware truncation, quant fan-in data-readiness guard (eliminates redundant LLM calls), CrewAI future annotations fix, case-insensitive username matching, `seed_user.py` test utility. |
| **—** | v2.6 | Analytics agent (PydanticAI, :8005) and Reviewer agent (OpenAI Agents SDK, :8006). Two-phase orchestration (Phase 1 parallel + Phase 2 review + Phase 3 synthesis). AG-UI eval hook. Reviewer input/output guardrails. |
| **—** | v2.7 | Shared SQLite agent_output store for cross-process data sharing. Reviewer simplified to synthesis-only (tools called directly in Python). Agent summaries passed to LLM. 10+ new HTML report sections (technicals, trends, forecast/MC charts, DCF, stress, signals, anomalies, stats, cross-validation). Chart.js visualizations. Frontend auth bypass via `NEXT_PUBLIC_AUTH_ENABLED=false`. Test fixes for Windows file locking and DuckDuckGo mock patterns. |
| **—** | v2.8 | `SendMessageInput(agent_name, ticker)` Pydantic model eliminates ticker hallucination. Pre-reviewer metric integrity gate (`validate_metric_integrity()`). Report calculation fixes (Sharpe risk-free rate, momentum double-multiply, MAPE format, stress test division, Beta label, MC BUY→HOLD downgrade). Unified `resolve_and_validate_ticker()` across all agents. Trace context nesting fix. RAG stream simplified to single-yield pattern. Reviewer tool expansions (confidence `_derive_*`, contradiction cross-checks, validation expansion). Observability dashboard replaces trace page. Web UI agent response capture (`_collect_agent_extra()`). Lazy import reversal (removed `__getattr__` pattern). Analytics & Reviewer frontend tiles. AG-UI keepalive streaming. `EVAL_TRACE_ENABLED` consolidated to `EVAL_ENABLED`. Eval gate timer reset on `defer_eval()`. |
