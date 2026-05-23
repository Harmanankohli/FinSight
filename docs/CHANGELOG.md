# Changelog

## v1.21 — Runtime RAGAS Robustness & Debuggability

### RAGAS Client Caching

- **`_setup_ragas_clients()` now caches** (`shared/runtime_eval.py`): Module-level `_ragas_clients` tuple stores `(InstructorLLM, _STEmbeddings)` after first call. Subsequent calls return cached clients instead of reloading `SentenceTransformer(all-MiniLM-L6-v2)` (~1-2s, ~80MB) on every agent response. Eliminates 4× model reload per query.

### Per-Metric Streaming

- **`_run_metrics` switched to `asyncio.wait(FIRST_COMPLETED)`** (`shared/runtime_eval.py`): Replaced `asyncio.gather` (waits for all metrics). Each metric is now logged and pushed to Langfuse the moment its `ascore` finishes. Fast metrics (AnswerRelevancy, DomainSpecificRubrics ~3-5s) appear immediately instead of waiting for slow metrics (Faithfulness ~180s timeout).

### Error Handling

- **`BaseException` instead of `Exception`** in `_run_metrics` result loop: `CancelledError` inherits from `BaseException`, not `Exception` — the old `isinstance(result, Exception)` check missed cancelled tasks, fell through to `round(float(result), 4)`, crashed with `TypeError`, and silently killed the entire eval via `create_task` fire-and-forget. Fixed by checking `isinstance(result, BaseException)`.
- **`float()` conversion guarded** with `try/except (TypeError, ValueError)` — any unexpected result types are logged instead of crashing.
- **`_score_metric` try/except** added: wraps `metric.ascore()` and logs full traceback with `exc_info=True` when a metric fails internally.
- **All scoring functions wrapped in try/except**: Orchestrator, Sentiment eval bodies catch unexpected exceptions and log them with full traceback instead of silently disappearing.

### Debuggability

- **Entry logs added**: Each scoring function logs `[agent] Eval entered (response_len=..., trace=...)` at INFO level on entry, confirming the function was reached.
- **Early-return warnings**: Silent `return` on short responses or import failures now logs `[agent] Skipping eval: ...` with reason.
- **Fallback logs promoted**: `logger.debug("[agent] No RAGAS scores computed")` → `logger.info` — visible at default log level.

### Timeout & Encoding Fixes

- **HTTP timeout 60s → 180s** (`shared/runtime_eval.py`): `AsyncOpenAI(timeout=180)` — Faithfulness makes multiple sequential LLM calls (decompose claims → verify each), each taking ~20-30s on the 20B model. The old 60s timeout failed on the second call.
- **UTF-8 stdout/stderr** (`shared/config.py`): `sys.stdout.reconfigure(encoding='utf-8')` prevents `UnicodeEncodeError` when RAGAS log messages containing curly quotes (`\u2010`, `\u2011`) hit Windows cp1252 console.

### Langfuse Push Cleanup

- **`_push_scores` skips when trace_id is None** (`shared/runtime_eval.py`): With placeholder Langfuse API keys (`pk-lf-...`), `create_score()` with no `trace_id` resulted in "Bad request" API errors. Now returns early when `trace_id is None`.

### Sentiment Narrative Key Fallback

- **`narrative` key fallback** (`agent_4_crewai/executor.py`): CrewAI LLM may return JSON with `investment_narrative` or `analysis` instead of `narrative`. The eval now tries `narrative` → `investment_narrative` → `analysis` → full JSON dump before giving up.

### Gitignore

- **`tests/evaluation/eval_results/` added to `.gitignore`**: Runtime-generated trace JSON artifacts excluded from version control.

## v1.20 — Runtime RAGAS Evaluation & Offline HF Model Loading

### Runtime RAGAS Evaluation

- **`shared/runtime_eval.py` (new)**: Fire-and-forget RAGAS scoring for all four agents as background tasks. Uses `ragas` metrics without requiring ground-truth references — scores are computed at runtime using the LLM itself as the judge.

- **Orchestrator scoring** (`agent_1_adk/agent_executor.py`): After response processing, fires `asyncio.create_task(_eval_score_response(...))`. Metrics: `ResponseRelevancy`, `citation_quality` (AspectCritic), `risk_disclosure` (AspectCritic), `recommendation_clarity` (RubricsScoreWithoutReference), `response_completeness` (AspectCritic).

- **RAG agent scoring** (`agent_2_llamaindex/executor.py`): After query response, fires `asyncio.create_task(_eval_rag_response(...))`. Metrics: `Faithfulness`, `ResponseRelevancy`, `LLMContextPrecisionWithoutReference`. Requires `context_texts` from ChromaDB source nodes.

- **Quant agent scoring** (`agent_3_langgraph/executor.py`): After analysis, fires `asyncio.create_task(_eval_quant_response(...))`. Metrics: `FactualCorrectness` (uses computed metrics as reference — catches hallucinated numbers), `ResponseRelevancy`.

- **Sentiment agent scoring** (`agent_4_crewai/executor.py`): After narrative, fires `asyncio.create_task(_eval_sentiment_response(...))`. Metrics: `ResponseRelevancy`, `catalyst_identification` (AspectCritic), `insider_signal_discussion` (AspectCritic), `Faithfulness` (when news/filing contexts available).

- **Score push to Langfuse**: All scores pushed to Langfuse `create_score()` per-trace, linked by `trace_id` when available. Enables regression tracking across model/prompt changes.

- **LM Studio compatibility patched**: RAGAS defaults to `instructor.Mode.JSON` which sends `response_format.type="json_object"` — LM Studio only supports `"json_schema"` or `"text"`. Patched with `instructor.Mode.JSON_SCHEMA` in `_setup_ragas_clients()`. HuggingFace embeddings wrapped via custom `_STEmbeddings` (RAGAS 0.4.x `BaseRagasEmbedding`) to avoid broken pydantic integration.

- **Eval trace directory updated** (`agent_1_adk/sub_agent_client.py`): Changed from `eval_traces/` to `tests/evaluation/eval_results/orchestrator_traces/` to align with test suite layout.

### CrewAI Simplification

- **Sentiment crew reduced from 2 agents to 1** (`agent_4_crewai/crew.py`): Removed separate Synthesis Agent — the Analysis Agent now produces the full narrative directly. `build_crew()` simplified from a 2-agent `Crew` with `sequential` process to a single-agent `Crew`. Reduces LLM calls per sentiment query from 2 to 1.

### Offline HuggingFace Model Loading

- **`HF_HUB_OFFLINE=1` default** (`shared/config.py`): Set at import time before any HuggingFace code runs. Prevents network calls to `huggingface.co` when loading `sentence-transformers` or `all-MiniLM-L6-v2` — models are expected to be cached locally from a prior online run. Set `HF_HUB_OFFLINE=0` in `.env` to re-enable download checks.

### Index Manager Cleanup

- **Duplicated query methods removed** (`agent_2_llamaindex/index_manager.py`): `query_sec_filings()` and `query_earnings()` were dead code — the RAG agent only calls `query()` (which routes via `RouterQueryEngine` with fallback). Removed both methods along with `query_earnings` index collection setup.

### Configuration

- **`A2A_TIMEOUT` default reduced** (`shared/config.py`): Changed from `300.0` to `180.0` — 5 minutes was excessive for local LLM inference; 3 minutes provides sufficient margin while failing faster on genuinely stuck agents.

## v1.19 — MCP Connection Cleanup & Server Script Fixes

### MCP Connection Cleanup

- **`_disconnect()` added to all sub-agent executors** (`agent_2_llamaindex/executor.py`, `agent_3_langgraph/executor.py`, `agent_4_crewai/executor.py`): New `async def _disconnect()` method calls `mcp.disconnect_all()` in a `try/finally` block, ensuring MCP sockets close gracefully after each analysis stream completes. Prevents `ConnectionResetError: [WinError 10054]` on Windows caused by lingering async sockets.
- **Orchestrator temporary MCP cleanup** (`agent_1_adk/agent_executor.py`): Pre-flight ticker validation's temporary MCP connection now wrapped in `try/finally` with `await _mcp.disconnect_all()` in the `finally` block.
- **All four agents disconnect MCP after stream**: Quant, RAG, Sentiment agents call `await self._disconnect()` in their `finally` blocks. Orchestrator cleans up the ticker-validation MCP client after use.

### Server Script Fixes

- **`run_adk_web.bat`**: Changed `cmd /c` to `cmd /k` for all server start commands — terminal windows stay open if a server crashes, allowing error inspection.
- **`stop_servers.bat`**: Rewrote window-closing logic. Switched from unreliable `taskkill /fi "WINDOWTITLE eq"` to PowerShell `Get-Process cmd | Where-Object { $_.MainWindowTitle -like 'FinSight*' } | Stop-Process -Force`, which reliably closes terminal windows by title.

### Bug Fixes

- **Date placeholder removed from ADK prompt** (`agent_1_adk/agent.py`): Removed `{date}` template variable from the orchestrator system prompt — the date was not being populated, leaving a raw `{date}` string visible in the LLM context.

## v1.18 — Caching, Guardrails, Evaluation & Observability

### Caching

- **TTL tool-result cache in MCP server** (`mcp_servers/finsight_server.py`): `_TTLCache` class using `OrderedDict` + `time.monotonic()`. Cache instances per tool: `get_prices` (5 min), `get_financials` (24 h), `get_news_sentiment` (15 min), `get_filing_content` (permanent LRU-200), `_fetch_submissions` (6 h). No new dependencies.
- **LangChain SQLiteCache** (`agent_3_langgraph/nodes.py`): `SQLiteCache(database_path=".langchain_cache.db")` wraps the quant agent's LLM summary call — identical ticker+metrics inputs reuse the cached LLM response. Requires `langchain-community>=0.3.0`.
- **KV cache prefix optimization** (`agent_1_adk/agent.py`, `agent_4_crewai/crew.py`): Static PROCEDURE block extracted to module-level `_STATIC_PREAMBLE` constant. `_build_instruction()` now only appends today's date and the dynamic agent list, keeping the large static prefix stable across requests for LM Studio KV-cache reuse. Backstory strings for CrewAI agents moved to module-level constants.
- **Semantic cache** (`shared/semantic_cache.py`): ChromaDB + `all-MiniLM-L6-v2` cosine similarity cache (threshold 0.95, TTL 1 h). Wired into `agent_1_adk/agent_executor.py`: cache checked before `runner.run_async`, hit returns immediately; successful responses stored. Controlled by `SEMANTIC_CACHE_ENABLED=true` env var (off by default).

### Guardrails

- **Input guardrails** (`agent_1_adk/agent_executor.py`): Off-topic regex filter (`_NON_INVESTMENT_RE`) rejects weather/recipe/entertainment queries with a canned message in < 100 ms. Pre-flight ticker validation calls MCP `validate_ticker` before spawning sub-agents — invalid tickers rejected in < 2 s with no sub-agent cost.
- **Output guardrails** (`agent_1_adk/agent_executor.py`): Empty/short response (< 50 chars) marked `TASK_STATE_FAILED`. Missing BUY/HOLD/SELL signal on a stock analysis query logs a Langfuse warning with `missing_signal: true` metadata.
- **Double-`else` syntax bug fixed** (`agent_1_adk/agent_executor.py`): Two `else` clauses for the same `if final_event:` block collapsed into one, fixing a `SyntaxError` that prevented the service from starting.

### RAG & Memory

- **Incremental RAG ingestion** (`shared/memory/store.py`, `agent_2_llamaindex/executor.py`): New `ingested_filings` table tracks already-indexed SEC filing URLs. `_ensure_ingested()` skips URLs already in the table; marks new ones after successful batch ingest. Persists across restarts — immutable historical filings are never re-ingested.
- **Embedding model pre-warm** (`agent_2_llamaindex/server.py`): `FinancialIndexManager` instantiated in a thread executor via `on_startup` hook, eliminating first-query latency caused by lazy model download.
- **Live price capture in PerformanceTracker** (`shared/memory/performance_tracker.py`): `record_recommendation()` now auto-fetches current price via `yfinance` in a thread executor when `price=None`. Enables accurate `realized_return` calculation in `evaluate_all()`.
- **Automated past-recommendation evaluation** (`agent_1_adk/agent.py`): `save_brief()` now fires `asyncio.create_task(_evaluate_past_recommendations(ticker))` in background — `PerformanceTracker.evaluate_all()` runs without blocking the response.
- **SQLite schema v2** (`shared/memory/store.py`): `ingested_filings` table added with `idx_ingested_ticker` index. `SCHEMA_VERSION` bumped to 2 for clean migration.

### Evaluation

- **RAGAS evaluation pipeline** (`tests/evaluation/`): `run_rag_eval.py` measures Faithfulness, ResponseRelevancy, ContextPrecision, ContextRecall, NoiseSensitivity. `run_orchestrator_eval.py` measures ToolCallAccuracy and AgentGoalAccuracy via eval traces written by sub-agent client. `financial_rubrics.py` provides custom `AspectCritic` metrics: citation quality, risk disclosure, recommendation clarity. `push_scores.py` pushes all scores to Langfuse per-trace.
- **Eval trace capture** (`agent_1_adk/sub_agent_client.py`): When `EVAL_TRACE_ENABLED=true`, each sub-agent call appends `{agent_name, task_sent, response, latency_ms}` to a JSON file in `tests/evaluation/eval_results/orchestrator_traces/`.
- **Curated RAG dataset** (`tests/evaluation/rag_dataset.json`): 10 Q&A pairs for NVDA, AAPL, MSFT, JPM with reference contexts from real SEC filings.

### Observability

- **LangGraph / LangChain instrumentation** (`agent_3_langgraph/server.py`): `LangChainInstrumentor().instrument()` added — quant agent LLM calls now appear in Langfuse traces. Requires `openinference-instrumentation-langchain>=0.1.0`.
- **Sub-agent latency tracking** (`agent_1_adk/sub_agent_client.py`): `send_message()` now records wall-clock latency per sub-agent call and emits a Langfuse span with `latency_ms` and agent name metadata.
- **Config validation** (`shared/config.py`): `validate()` function checks required env vars (`MCP_SERVER_URL`) and warns on placeholder Langfuse keys. Called at startup in each server entry point.

### Deployment

- **Health endpoints** (`agent_1_adk/main.py`, `agent_2_llamaindex/server.py`, `agent_3_langgraph/server.py`, `agent_4_crewai/server.py`, `mcp_servers/finsight_server.py`): All five services expose `GET /health → {"status":"ok","agent":"..."}`. MCP server mounts health alongside the SSE app via a Starlette wrapper.
- **docker-compose hardening** (`docker-compose.yml`): All services gain `healthcheck` blocks. `depends_on` updated to `condition: service_healthy`. `finsight_memory` named volume added for the orchestrator DB. `SEMANTIC_CACHE_ENABLED=false` added to `agent-adk` env.
- **Orchestrator Dockerfile** (`agent_1_adk/Dockerfile`): New container image following `agent_3_langgraph/Dockerfile` pattern — Python 3.12-slim, copies `agent_1_adk/` + `shared/`, exposes port 8001.
- **`langchain-community>=0.3.0`** and **`openinference-instrumentation-langchain>=0.1.0`** added to `pyproject.toml` deps. **`ragas>=0.2.0`** added to `[project.optional-dependencies] dev`.

## v1.17 — Centralized File Logging

- **`shared/logging_config.py` added**: `setup_file_logging(service_name)` configures the root logger with a `StreamHandler` (stderr) and a `RotatingFileHandler` (10 MB, 5 backups). Safe to call multiple times — duplicate handlers are skipped.
- **All services write to `logs/`**: Orchestrator → `logs/orchestrator.log`, RAG → `logs/rag_agent.log`, Quant → `logs/quant.log`, Sentiment → `logs/sentiment.log`, MCP → `logs/mcp.log`. Directory is created automatically if absent.
- **`memory_callback.log` moved to `logs/`**: Was written to the project root; now at `logs/memory_callback.log`.
- **`basicConfig` removed from all servers**: Stale `logging.basicConfig(level=logging.INFO)` calls replaced by module-level `setup_file_logging(...)`, so logging is configured whether the server is imported (uvicorn) or run directly.

## v1.16 — Code Streamlining & Bug Fixes

- **Ticker validation consolidated**: `_validate_ticker()` and `_resolve_ticker()` were copy-pasted verbatim (~108 LOC) across all three sub-agent executors. Extracted as `validate_ticker_via_mcp(mcp, ticker)` and `resolve_ticker_via_mcp(mcp, query, exclude_ticker)` in `shared/ticker_utils.py`. Each agent's methods are now ~7-line wrappers.
- **Dead config vars removed from `shared/config.py`**: `RAG_AGENT_URL`, `QUANT_AGENT_URL`, `SENTIMENT_AGENT_URL` (superseded by `AGENT_SEED_URLS`) and `ORCHESTRATOR_PORT`, `RAG_PORT`, `QUANT_PORT`, `SENTIMENT_PORT` (hardcoded in each server file, never imported from config).
- **`import json` inside loops fixed**: `agent_3_langgraph/executor.py` and `agent_4_crewai/executor.py` had `import json` inside `try` blocks inside loops; hoisted to module level.
- **RAG agent MCP connection refactored**: Inline connect pattern repeated in `_ensure_ingested`, `_validate_ticker`, and `_resolve_ticker` consolidated into a single `_ensure_mcp_connected()` helper.
- **Correlation matrix auto-trigger fixed**: Stored portfolio holdings from memory context were silently injected into every query, causing the quant agent to compute a full correlation matrix even for single-ticker requests. Fixed by (1) labelling the memory-context portfolio line as background reference and (2) updating the orchestrator prompt to only pass holdings to the quant agent when the user explicitly requests portfolio correlation in their current message.

## v1.15 — Dead Code Cleanup

- **`shared/types.py` removed**: Defined `ServerConfig`, `PlannerTask`, `TaskList`, `AgentResponse` — none were imported or referenced by any file in the project. These types were superseded by models in `shared/models.py`.
- **`shared/workflow.py` removed**: Defined `WorkflowGraph`, `WorkflowNode`, `Status` — never imported by any production code. The system uses LangGraph for the quant agent's state machine instead.
- **`tests/test_workflow.py` removed**: 8 tests for the unused `WorkflowGraph` implementation.
- **`ui/memory_test.html` and `ui/test.html` removed**: Standalone HTML pages with zero references from any source or configuration.
- **README updated**: Removed stale reference to `shared/types.py` from project structure diagram.
- **TESTS.md updated**: Test count corrected from 72 → 64.

## v1.14 — `load_memory` Fix & RAG Timeout Optimization

- **`load_memory` now returns results**: Root cause was `SQLiteMemoryService.add_events_to_memory()` requiring `app_name` and `user_id` as mandatory args, but ADK's `Context.add_events_to_memory()` only passes `events` and `custom_metadata`. Fixed by making `app_name` and `user_id` optional with defaults, and extracting them from `custom_metadata` when not provided directly.
- **`after_agent_callback` signature corrected**: ADK's `CallbackContext.add_events_to_memory()` takes `(self, *, events, custom_metadata=None)` — not `(app_name, user_id, events, session_id)`. Updated `agents/finsight_agent/agent.py` to pass events via `custom_metadata` with user_id, session_id, and app_name embedded.
- **Dual persistence path**: Events are now persisted to `memory_entries` both via `after_agent_callback` (ADK web UI path) and `_persist_to_memory` (A2A executor path), ensuring memory works regardless of how the agent is invoked.
- **`_persist_to_memory` added to `agent_executor.py`**: After each successful response, events are directly persisted to the runner's memory service. This bypasses the unreliable callback chain for A2A requests.
- **RAG retrieval deduplication**: Reduced `similarity_top_k` from 5 → 3 across all index query engines in `index_manager.py` to cut context size and LLM inference time by ~40%.

- **`DatabaseSessionService` replaces `InMemorySessionService`**: ADK's built-in `DatabaseSessionService` with `sqlite+aiosqlite:///./finsight_memory.db` provides persistent session/event storage across restarts. Full conversation history (user messages, agent responses, tool calls) is saved to SQLite.
- **`SQLiteMemoryService` for cross-session memory search**: Custom implementation of ADK's `BaseMemoryService` that persists conversation events to SQLite. The `load_memory` tool can search past conversations across sessions and restarts. Sessions are auto-ingested after each successful response.
- **`TickerMemory` for structured brief history**: Stores per-ticker investment recommendations with ticker, recommendation (BUY/HOLD/SELL), confidence, full response text, and timestamp. Provides `format_context()` that generates a compact (~300 token) memory summary injected into the orchestrator's system prompt before each query.
- **`PortfolioStore` for user profile persistence**: Auto-captures portfolio holdings from each query's context. Merges holdings over time — users never need to explicitly set their portfolio. Stores risk profile and investment horizon.
- **`PerformanceTracker` for recommendation outcomes**: Records each BUY/HOLD/SELL recommendation with optional price snapshot. Can evaluate past recommendations against current market prices via yfinance. Provides accuracy stats (win rate by recommendation type).
- **Memory context injection**: Before each query, the executor extracts the ticker, retrieves the latest recommendation from `TickerMemory`, and prepends it to the user message. This enables the LLM to answer "Has the outlook for NVDA changed since last time?"
- **Auto-save on every response**: `agent_executor.py` automatically stores briefs, recommendations, and portfolio updates after every successful response — no LLM action required.
- **`save_brief` tool removed**: Simplified to auto-save only. The LLM no longer needs to explicitly call a tool to persist its analysis.
- **`load_memory` tool added to orchestrator**: The ADK `load_memory` tool is now available to the orchestrator LLM for searching past conversations.
- **`finsight_memory.db` added to `.gitignore`**: SQLite database file excluded from version control.
- **16 tests passing** in `tests/test_memory.py`: covers all four memory stores (TickerMemory, PortfolioStore, PerformanceTracker, SQLiteMemoryService) plus the SQLite foundation.

## v1.12 — A2A Span Noise Filtering

- **Noisy A2A internal spans filtered**: Replaced `should_export_span=lambda span: True` with `is_default_export_span` from `langfuse.span_filter`. A2A SDK internal spans (`a2a-python-sdk` instrumentation scope) and HTTPX transport spans are no longer exported to Langfuse, keeping traces clean and focused on high-level workflow steps and LLM calls.
- **What's preserved**: `finsight-query` root traces, `orchestrator-execute`, `rag-agent-stream`, `quant-agent-stream`, `sentiment-agent-stream`, and all LLM spans (LiteLLM, LlamaIndex, LangChain, CrewAI).
- **What's filtered**: A2A `send_message` internals, `DefaultRequestHandler`, HTTPX transport spans, and other infrastructure spans.

## v1.11 — Portfolio Holdings Extraction & Correlation Matrix Fix

- **`extract_holdings()` added to `shared/ticker_utils.py`**: Extracts portfolio holdings from natural language queries using 4 regex patterns covering common phrasing: "My portfolio holds AAPL, MSFT", "I own MSFT and GOOGL", "My portfolio: TSLA, AMZN, META", "My current holdings are JPM, BAC, WFC".
- **Holdings passed through Quant agent chain**: `stream()` → `analyze(portfolio_holdings=...)` → `graph.run(portfolio_holdings=...)` → `correlation_node`. Previously holdings were always `None` regardless of user input.
- **Correlation matrix now returns helpful notes**: Instead of empty `{}`, returns `{"note": "No portfolio holdings provided..."}` when no holdings mentioned, and `{"error": "..."}` when correlation computation fails.
- **Orchestrator LLM instructed to pass holdings**: Updated orchestrator system prompt (step 4) telling the LLM to include portfolio holdings in the task text for the Quant Analysis Agent.
- **6 new tests for `extract_holdings()`**: Covers portfolio holds, colon syntax, and/or connector, no holdings mentioned, exclude target ticker, current positions phrasing.
- **14 total tests in `test_trace_propagation.py`**: 8 trace propagation + 6 holdings extraction.

## v1.10 — Langfuse Distributed Tracing Fix

- **Cross-process trace propagation fixed**: Sub-agent spans now correctly link to the orchestrator's root trace instead of creating orphan traces. Each agent process previously created its own root trace because `start_observation(trace_context=...)` was not properly linking spans across process boundaries.
- **`extract_trace_ids()` helper added**: New function in `shared/trace_context.py` that returns `(trace_id, parent_span_id, clean_query)` — a convenience wrapper over `extract_trace_context()` for the common case of needing explicit IDs.
- **`start_observation()` with explicit `trace_context`**: All three sub-agents (RAG, Quant, Sentiment) now use `langfuse.start_observation(..., trace_context=trace_ctx)` where `trace_ctx` is built from the injected `trace_id` and `parent_span_id` passed through the A2A message text prefix.
- **`CallbackHandler(trace_context=...)` for LangGraph**: Quant agent passes `trace_context` dict to Langfuse's LangChain `CallbackHandler` so internal graph nodes (fetch_prices, compute_metrics, dcf_valuation, llm_summary) are also linked to the parent trace.
- **`start_observation` over `start_as_current_observation`**: Used `start_observation()` (manual, no OTel context management) instead of `start_as_current_observation()` (context manager) because the latter conflicts with async generators — OTel context tokens are created in a different async context, causing `ValueError: Token was created in a different Context`.
- **Trace context injection unchanged**: `inject_trace_context()` in `sub_agent_client.py` already serialized `trace_id` + `parent_span_id` as a JSON prefix in the A2A task text. The fix was on the extraction/usage side.
- **8 trace propagation tests passing**: Added `test_extract_trace_ids_roundtrip` and `test_extract_trace_ids_no_context` to verify the new helper.

## v1.9 — Logging Overhaul & DCF Skip Messaging

- **Comprehensive logging added**: 11 new `logger.info/warning/debug` calls across `graph.py`, `nodes.py`, and `executor.py` — routing decisions, metric computation failures, DCF fallbacks, beta calculation errors, format-output summaries, and execution lifecycle
- **`dcf_error` now set on high-volatility route**: `compute_metrics_node` sets `dcf_error` when volatility > 35% so callers see why DCF was skipped (e.g. "DCF skipped: annual volatility (41.0%) exceeds 35% threshold – routed to stress test instead")
- **`dcf_error` included in graph output**: `graph.run()` now returns `dcf_error` in its result dict — previously only kept in state, never surfaced
- **`dcf_error` added to reasoning**: `format_output_node` includes `dcf_error` in the reasoning string when DCF is null with an error, so the LLM summary has context
- **`dcf_error: None` in initial state**: Added missing field to graph initial state for TypedDict consistency
- **`quant.log` routing diagnostics**: `_route_on_volatility` logs which branch was taken with ticker + volatility value
- **FCF debug logging**: `_get_fcf_from_financials` logs every FCF candidate examined per period (debug level) — no more silent "null" on cash flow parsing

## v1.8 — Documentation & Housekeeping

- **All docs updated**: README, TESTS, ARCHITECTURE, AGENTS, MCP_SERVERS, DEMO, CHANGELOG, DESIGN_DECISIONS synced with codebase
- **README diagram expanded**: MCP tool list updated to include all 13 tools (added `get_options_chain`, `get_financial_filings`, `get_filing_content`, `validate_ticker`, `resolve_company_ticker`, `get_earnings_calendar`)
- **README shared section**: Added `ticker_utils.py` to project structure
- **TESTS.md**: Corrected test count from 39→42, added `test_orchestrator_tools.py`, updated per-file test counts
- **ARCHITECTURE.md**: LLM models updated from `gpt-oss-20b` to `qwen/qwen3-30b-a3b-2507`; MCP tool diagram expanded
- **MCP_SERVERS.md**: Added `get_financial_filings`, `validate_ticker`, `resolve_company_ticker` tool documentation
- **DEMO.md**: Updated to reflect parallel calling with qwen model; corrected tool description to single `send_message`

## v1.7 — MCP Server Refactoring & News Fallback

- **`_EdgarClient` refactored**: Extracted `_build_filing_urls()` and `_fetch_submissions()` methods. `_INDEX_ONLY_FORMS` moved to module level. Added `FINANCIAL_FORM_TYPES` constant.
- **New `get_financial_filings` tool**: Fetches 10-K/10-Q filings separately with balanced annual/quarterly limits. Prevents the common failure of `get_company_filings` returning mostly 8-Ks. Separates annual from quarterly in response.
- **RSS fetching rewritten**: `_fetch_rss()` now returns a structured dict (`entries`, `status`, `error`) instead of a raw feedparser object. All three feeds fetched concurrently via `asyncio.gather`. Each source gets individual `feed_status` diagnostics.
- **Yahoo Finance news API fallback**: `_fetch_yf_news()` tries Yahoo Finance's structured news search when all RSS feeds are unreachable or return zero matching articles. Results are pre-filtered to the ticker — no keyword matching needed.
- **`get_news_sentiment` enhanced**: Added `feed_status` (per-source diagnostics), `source_used` field ("rss" / "yahoo_finance_api" / "none"), and improved error messaging. Distinguishes rss_unreachable vs rss_no_match.
- **Keyword matching improved**: Extracted `_resolve_company_keywords()` to build ticker+company-name keyword lists with bigrams and split-at-2 variants. `_keyword_matches()` uses word boundaries for single-word keywords to avoid substring false positives.

## v1.6 — DCF Robustness, Ticker False Positives & Date Awareness

- **DCF finds first positive FCF**: `_get_fcf_from_financials()` skips negative FCF periods instead of returning null. Logs detailed `dcf_error` at each failure point (no MCP client, empty cash flow, no positive FCF, missing shares/price).
- **`dcf_error` field in state**: New `QuantAnalysisState.dcf_error` captures exact DCF failure reason.
- **Financial stop-word blocklist**: `_FINANCIAL_STOP_WORDS` filters out "SEC", "EPS", "CEO", "NYSE", "NASDAQ", "INC", "GAAP" from regex ticker matches — prevents "Analyze GE SEC filings" from extracting "SEC" instead of "GE".
- **Query noise cleanup**: `clean_query_for_resolution()` strips analysis noise words before sending to MCP's `resolve_company_ticker`. Failed ticker candidates are excluded from the resolution query.
- **Date hallucination fix**: All LLM prompts now include `Today's date: {current_date}` — orchestrator, RAG queries, quant summary, and sentiment crew.
- **MCP: `follow_redirects=True`**: httpx client follows 301 redirects automatically.
- **MCP: CIK leading-zero fix**: EDGAR archive URLs use `str(int(cik))` instead of 10-digit zero-padded CIK, preventing 301 redirects.
- **MCP: IXBRL viewer pages parsed**: Removed "XBRL Viewer" skip — pages parsed with `html.parser`. `lxml-xml` fallback to `html.parser`.
- **MCP: MarketWatch RSS updated**: `feeds.content.dowjones.io/public/rss/mw_topstories`.

## v1.5 — Company Name to Ticker Resolution

- **New `resolve_company_ticker` MCP tool**: Resolves natural language company names ("Mastercard", "Apple") to ticker symbols via SEC reverse index (instant, local cache) with Yahoo Finance search API fallback
- **`_resolve_ticker()` on all agents**: When regex `extract_ticker()` returns empty, agents now call MCP `resolve_company_ticker` before giving up — handles "analyze Mastercard" → "MA" correctly
- **Fixed `re.IGNORECASE` bug in `extract_ticker()`**: Added `.isupper()` guard to prevent lowercase words ("the", "in") from being captured as tickers
- **Pattern 4 changed from `matches[-1]` to `matches[0]`**: The last-matching heuristic picked up trailing stop words ("SEC", "EPS") over the actual ticker. First-match prefers the ticker, which typically appears earlier in LLM-generated task text. Falls back to company name resolution if wrong.
- **`is_valid_ticker_format()` guard**: New shared function rejecting non-standard tickers (digits, periods, >5 chars). Applied in all agents' `_resolve_ticker()` to prevent mutual fund identifiers from reaching validation.
- **`_validate_ticker` fallback to resolution**: When regex-extracted ticker fails SEC validation, agents retry with company name resolution before returning an error.

## v1.4 — Ticker Extraction Decoupled from SEC Validation

- **`extract_ticker()` simplified**: `shared/ticker_utils.py` is now pure regex — no SEC API calls, no `httpx` dependency, instant execution. All SEC validation moved to MCP server.
- **`_validate_ticker()` added to all agents**: Consistent `tuple[bool, str, str]` return type across agent_2 (RAG), agent_3 (Quant), and agent_4 (Sentiment). Connects MCP, calls `validate_ticker` tool, falls back to regex guess on MCP failure.
- **Fixed broken `_connect()` call in agent_2**: `RAGAgent.stream()` was calling `await self._connect()` which didn't exist. Now uses `_validate_ticker()`.
- **Error message for missing ticker**: All agents now show: *"Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V)."*
- **MCP `validate_ticker` pre-warming**: SEC ticker map pre-loaded on first tool call (`_prewarm_ticker_map()`) so subsequent validations are instant.
- **All agents handle MCP downtime**: If MCP connect or `validate_ticker` call fails, agents log a warning and proceed with the regex-extracted ticker (best-effort).

## v1.3 — MCP Server Fixes & RAG Content Ingestion

- **Docker-compose MCP fix**: Replaced 4 broken MCP services (`mcp-yfinance`, `mcp-sec-edgar`, `mcp-reddit`, `mcp-python-runner`) that referenced non-existent files with single unified `finsight-mcp` service
- **New `get_filing_content` tool**: Added MCP tool to fetch and extract text from raw SEC EDGAR filing URLs with IXBRL fallback
- **RAG content ingestion fix**: RAG agent now fetches actual filing content (10-K, 10-Q, 8-K text) via `get_filing_content()` instead of just storing metadata. Returns raw document URL (`edgar_url`) plus viewer fallback (`ix_url`)
- **Quant DCF fix**: DCF valuation now correctly reads free cash flow from `cash_flow` statement instead of `income_statement` (was returning null)
- **MCP response parsing utility**: Added `parse_mcp_result()` to `shared/mcp_client.py` for consistent handling across all agents

## v1.2 — Model Update & MCP Server Hardening

- **Model change**: All agents migrated from `gpt-oss-20b` to `qwen3-30b-a3b-2507` — ~5-10x faster inference per LLM call
- **`.env` / `.env.example`**: Updated `LLM_MODEL` and `ADK_MODEL` defaults to qwen

- **Windows compatibility**: `import resource` guarded by `sys.platform != "win32"` check
- **Lazy agent registry**: Model download deferred to first tool call (`_ensure_registry`), no blocking at import time
- **Thread-safe SSE app**: `get_app()` with double-checked locking (`_starlette_app` singleton)
- **Inline imports**: `import re as _re` inside `_resolve_company_keywords` and `_normalise_for_match` for localised scope
- **NaN/Inf serialisation**: `_serialise_value` handles NaN, infinity, numpy types, datetimes
- **EDGAR caching**: In-memory CIK/ticker/title map with lock-protected lazy loading
- **Expanded sandbox restricted imports**: `builtins`, `gc`, `threading`, `multiprocessing`, `signal`, `mmap`, `resource`, `pwd`, `grp`, `crypt` added to blocklist
- **SEC earnings fallback**: `get_earnings_calendar` falls back to EDGAR XBRL when yfinance lacks data
- **Retry logic**: EDGAR company filings URL fetch uses 3-attempt exponential backoff
- **42 tests passing**

## v1.1 — A2A Protocol Alignment

- **A2A discovery**: Replaced sync raw HTTP with async `A2ACardResolver` — standard `/.well-known/agent-card.json`, protobuf `AgentCard` types, backwards compatibility
- **A2A client**: Replaced `create_client()` with `ClientFactory` — proper transport negotiation, matches official A2A SDK pattern
- **Single `send_message` tool**: Removed per-agent tool generation. LLM now uses one tool with `agent_name` parameter, matching all A2A sample projects (Google, bhancockio, theailanguage)
- **Removed `list_remote_agents`**: LLM already sees agents in the instruction prompt
- **Pre-fetch removed**: `FinSightAgentExecutor` no longer pre-fetches sub-agent data. LLM routes via `send_message` tool, matching A2A sample executor pattern
- **Streaming event handling**: Correctly skips SUBMITTED/WORKING events, captures `artifact_update` (data + text parts) and terminal `status_update` events
- **Background async discovery**: Supports both ADK Web UI (running event loop → `loop.create_task()`) and CLI (`asyncio.run()`)
- **Windows event loop fix**: `WindowsSelectorEventLoopPolicy` prevents noisy `ConnectionResetError`
- **Programmatic AgentCards**: All servers now build `AgentCard` in code using protobuf types — removed `agent_1_adk/agent_card.json`
- **Agent card descriptions**: Updated from "Ollama" to "LM Studio"
- **44 tests passing** (42 standard + 2 orchestrator tool tests removed with `list_remote_agents`)

## v1.0 — LM Studio Migration

- **Model change**: All agents migrated from Ollama (`qwen2.5:7b`) to LM Studio (`gpt-oss-20b`) — OpenAI-compatible local API
- **Config**: Removed `OLLAMA_BASE_URL`, changed `LLM_BASE_URL` default to `http://localhost:1234/v1`
- **Dependencies**: Replaced `llama-index-llms-ollama` with `llama-index-llms-openai-like`, `langchain-ollama` with `langchain-openai`
- **Agent 3 (Quant)**: Switched from direct `yfinance` calls to MCP tools (`get_prices`, `get_financials`)
- **Agent 2 (RAG)**: Removed static `mcp_config.yaml` — MCP server URL passed inline via `MCPServerConfig`
- **Agent 4 (Sentiment)**: Removed static `mcp_config.yaml` — same inline pattern
- **`.env`**: Cleaned up obsolete Ollama variables

## v0.9 — Model Migration to qwen2.5:7b

- **Model change**: All agents migrated from `llama3.2` to `qwen2.5:7b`
- **`.env.example`**: Updated default models

## v0.8 — Streamlined ADK Agent

- **ADK agent restructured**: Replaced legacy modules with clean `agent.py` + `sub_agent_client.py` + `agent_executor.py` + `main.py`
- **39 tests passing**

## v0.7 — v0.1

- Earlier iterations: model testing, MCP consolidation, initial A2A SDK integration, project scaffolding
