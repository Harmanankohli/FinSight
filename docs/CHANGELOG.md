# Changelog

## v1.27 — Security Sandbox Hardening + 60 AST-Gate Tests

### Sandbox Extraction & Hardening

- **`shared/sandbox.py` (new, 263 lines)**: Extracted the three-layer Python sandbox from `mcp_servers/finsight_server.py` into a dedicated shared module. `run_sandbox(code, timeout)` provides the same signature that `execute_python` called inline before.
- **Expanded import blocklist**: Added 20 new restricted modules: `shlex`, `concurrent`, `ssl`, `http`, `urllib`, `requests`, `ftplib`, `poplib`, `smtplib`, `telnetlib`, `xmlrpc`, `socketserver`, `pathlib`, `io`, `glob`, `fnmatch`, `tempfile`, `zipfile`, `tarfile`, `gzip`, `bz2`, `lzma`, `base64`, `codecs` — covering filesystem access, network protocols, and encoding-based escape vectors.
- **Windows-safe `resource` import**: Moved `import resource` inside `_sandbox_preexec()` (guarded by try/except) instead of a module-level `sys.platform` guard — avoids import errors on Windows while preserving Unix resource limits.
- **`mcp_servers/finsight_server.py`**: 230 lines removed — `_RESTRICTED_IMPORTS`, `_RESTRICTED_CALLS`, `_RESTRICTED_ATTRS`, `_check_code_safety()`, `_sandbox_preexec()`, `_SANDBOX_RUNNER`, and `execute_python` body replaced with `from shared.sandbox import run_sandbox as _run_sandbox` + a single delegation call.

### Security Test Suite

- **`tests/security/test_sandbox.py`** (~60 test cases): 45+ parametrized negative cases covering every restricted module, builtin, dunder attribute, getattr-with-dunder, and subscript-with-dunder pattern. 13 positive cases verifying safe code (math, json, list comprehensions, `isinstance`, `str`) is not blocked. Integration tests (marked `integration`) spawn the actual subprocess to verify runtime sandbox enforcement, timeout handling, and runtime import blocking.

## v1.26 — Pragmatic Test Suite (88 Unit Tests)

### New Test Suite

- **88 unit tests added** across 10 test files, covering all core primitives — models, quant graph nodes, ticker utilities, TTL cache, rate limiter, trace context, memory store, and ticker memory.
- **`tests/conftest.py` (new)**: Shared fixtures: `_clean_env` (autouse) monkeypatches `LLM_API_KEY`, `LLM_BASE_URL`, `LANGFUSE_*` for test isolation. `memory_db` fixture provides per-test isolated SQLite database by resetting the module-level connection singleton.
- **`tests/unit/test_models.py`** (10 tests): `QueryContext`, `RAGInsights`, `QuantMetrics`, `SentimentIntelligence`, `InvestmentBrief` — construction, serialization round-trip (`model_dump` → `model_validate`), optional fields, multiple recommendations.
- **`tests/unit/test_quant_graph_nodes.py`** (18 tests): All three LangGraph nodes tested directly with synthetic log-normal price data from `numpy.random.default_rng`. Covers Sharpe/VaR/max-drawdown correctness, high/low volatility branching, stress-test CVaR ≤ VaR invariant, empty-price edge cases, BUY/HOLD/SELL signal logic.
- **`tests/unit/test_ticker_utils.py`** (11 tests): Parametrized `is_valid_ticker_format`, `extract_ticker` with stop-word blocklist, `extract_holdings` with comma/and/colon syntax, `clean_query_for_resolution`.
- **`tests/unit/test_ttl_cache.py`** (9 tests): Cache miss/hit/expiry, single-flight dedup (N concurrent callers share one fetch), LRU eviction at `max_entries`, exception propagation.
- **`tests/unit/test_rate_limiter.py`** (4 tests): Burst consumption speed, rate enforcement after burst, token refill over time, burst=1 bucket timing.
- **`tests/unit/test_trace_context.py`** (8 tests): Inject/extract round-trip, missing prefix returns None, separator in task text, double injection, `current_trace_id` contextvar.
- **`tests/unit/memory/test_memory_store.py`** (5 tests): Table creation, idempotent `init_db`, schema version, WAL journal mode, required indexes.
- **`tests/unit/memory/test_ticker_memory.py`** (7 tests): Store/get_latest, case-insensitive ticker lookup, history retrieval, flip detection (`has_changed`), minimal store path.
- **`tests/integration/test_mcp_server_smoke.py`** (4 tests, marked `integration` + `external`): HTTP reachability of MCP server (port 8010) and agent card endpoints (ports 8002–8004).

### Configuration

- **`pyproject.toml`**: Added `asyncio_default_fixture_loop_scope = "function"`, custom pytest markers `integration` and `external`.

## v1.25 — Env Var Hardening & SQLite Connection Singleton

### Configuration: Env Vars for Secrets

- **`SEC_USER_AGENT` env var** (`shared/config.py`): Replaces hardcoded `"FinSight Research (contact@finsight.com)"` in `_SEC_HEADERS`. Defaults to a `"dev-mode-set-SEC_USER_AGENT"` placeholder with a startup warning when unset. Set `SEC_USER_AGENT=Your Name (your-email@example.com)` in `.env` for production.
- **`LLM_API_KEY` env var** (`shared/config.py`): Replaces `api_key="lmstudio"` hardcoded in 3 agent files. Defaults to `"lmstudio"` for backward compatibility with LM Studio. Enables switching to OpenAI/Anthropic by changing `.env` only.
- **Agent files updated**: `agent_2_llamaindex/index_manager.py`, `agent_3_langgraph/nodes.py`, `agent_4_crewai/crew.py` — import and use `LLM_API_KEY` from config.
- **MCP server updated** (`mcp_servers/finsight_server.py`): Imports `SEC_USER_AGENT` from config for the SEC EDGAR `_SEC_HEADERS`.
- **`.env.example` updated**: Documents both `LLM_API_KEY` and `SEC_USER_AGENT` with usage comments.

### SQLite Long-Lived Connection Singleton

- **Singleton connection** (`shared/memory/store.py`): `get_db()` now returns a module-level `_db_conn` singleton instead of opening a new connection per call. Double-checked locking via `_init_lock` prevents race conditions on first access.
- **Write lock** (`shared/memory/store.py`): New `write_lock()` function returns a module-level `asyncio.Lock`. All writers (`store_brief`, `store_minimal`, `update_response_text`, `upsert_from_context`, `update_holdings`, `record_recommendation`, `evaluate_all` updates, `add_session_to_memory`, `add_events_to_memory`, `mark_filing_ingested`) wrapped with `async with write_lock()`.
- **Reader cleanup**: All `try/finally + await conn.close()` removed from read paths — readers use the shared connection directly without close.
- **WAL + busy_timeout**: Set once at singleton init (no longer on every `get_db()` call).
- **`close_db()` added**: New function for process-shutdown cleanup.
- **Files changed**: `shared/memory/store.py`, `shared/memory/ticker_memory.py`, `shared/memory/portfolio_store.py`, `shared/memory/performance_tracker.py`, `shared/memory/memory_service.py` — 284 insertions, 304 deletions across 5 files.

### Token-Bucket Rate Limiter

- **`shared/rate_limiter.py` (new)**: `TokenBucket` class using `asyncio.Lock` + `time.monotonic()` with configurable rate and burst. Loop-based acquire waits exactly the deficit time instead of recursing.
- **SEC limiter** (`_sec_limiter`): 8 req/s, burst 10 — applied to 5 HTTP call sites in `_EdgarClient` (ticker map fetch, submissions, filing content, full-text search, EDGAR filing fetch). SEC's published limit is 10 req/s; 8/s leaves headroom.
- **yfinance limiter** (`_yfinance_limiter`): 4 req/s, burst 8 — applied to `get_prices`, `get_financials`, `get_options_chain`, `get_earnings_calendar`. Yahoo has no published cap; conservative rate avoids 429s.
- **RSS limiter** (`_rss_limiter`): 2 req/s, burst 4 — applied to `_fetch_rss` and `_fetch_yf_news` Yahoo fallback. News feeds are the least latency-sensitive.

### TTL Cache with Single-Flight Dedup

- **`shared/ttl_cache.py` (new)**: Async `TTLCache` class replacing the old threaded `_TTLCache`. Supports `get_or_fetch()` with single-flight dedup — N concurrent callers for the same key share one in-flight fetch, each receiving the result when done. Also exposes `get()`/`set()` for tools needing conditional caching.
- **Single-flight dedup**: Double-checked locking pattern — after acquiring the asyncio lock, re-checks cache to avoid redundant fetches on race wins. Pending fetches tracked in `_inflight` dict of `asyncio.Future` objects, created via `loop.create_future()`.
- **TTL updates**: Prices 5 min → 1 min (intraday prices change every trade), financials 24 h → 1 hr (quarterly data doesn't change intraday, but 24h was unnecessarily long; 1h matches typical session refreshes), news 15 min → 5 min (fresher headlines without hammering RSS).
- **Extracted uncached helpers**: `_get_prices_uncached` and `_get_financials_uncached` — the actual yfinance logic separated from the tool wrapper, making the cache layering explicit.
- **Filing/submission caches**: Keep their existing behavior (`_cache_filing` permanent LRU-200, `_cache_submissions` 6h) — now backed by `TTLCache` instead of `_TTLCache`.
- **Files changed**: `shared/ttl_cache.py` (new, 74 lines), `mcp_servers/finsight_server.py` (119 insertions, 78 deletions).

### Structured JSON Logging

- **`JsonFormatter` added** (`shared/logging_config.py`): New formatter for the file handler that writes one JSON object per line with keys: `ts`, `level`, `service`, `logger`, `message`, plus optional `trace_id`, `session_id`, `ticker`, `latency_ms` when set on the LogRecord. Exception tracebacks serialized as `exc` key.
- **StreamHandler kept plaintext**: Terminal output remains readable — only the file handler uses JSON. Prevents the "wall of JSON" problem in interactive terminals.
- **No code changes needed in callers**: `setup_file_logging(service_name)` signature unchanged. Existing callers (`setup_file_logging("orchestrator")`, etc.) automatically get JSON file logs after this change.

### Per-Service Log Levels via Env

- **Env-based log level resolution** (`shared/logging_config.py`): `setup_file_logging(service_name, level=None)` now reads `LOG_LEVEL_<SERVICE>` (e.g. `LOG_LEVEL_MCP`), falls back to `LOG_LEVEL`, then defaults to `INFO`. Callers can still pass `level=` explicitly to override env.
- **`.env.example` updated**: Documents `LOG_LEVEL` for global default and per-service override examples (`LOG_LEVEL_ORCHESTRATOR=DEBUG`, `LOG_LEVEL_QUANT=WARNING`).

### Log Sanitization Filter

- **`SanitizeFilter` added** (`shared/logging_config.py`): `logging.Filter` subclass with compiled regex patterns that scrub `api_key=` values, `sk-`/`pk-` tokens, `Bearer` authorization headers, and `LANGFUSE_PUBLIC/SECRET_KEY` values before the log line reaches any handler.
- **Attached to both handlers**: The filter is added to both the StreamHandler (terminal) and RotatingFileHandler (file), so secrets are never written to disk or displayed in console output.
- **Args scrubbing**: The `filter()` method also iterates `record.args` to catch formatted strings where secret values appear in `%s` placeholders (`"GET /api?key=%s" % secret`).

### SQLiteTaskStore Replacing InMemoryTaskStore

- **`shared/a2a_store.py` (new, 102 lines)**: `SQLiteTaskStore` implementing the A2A `TaskStore` protocol. Wraps `InMemoryTaskStore` for fast in-process get/list/delete and adds SQLite write-through via the `a2a_tasks` table. On cold start, all rows from SQLite are loaded into the in-memory store once — tasks survive process restarts.
- **Double-checked lazy load**: `_ensure_loaded()` uses an `asyncio.Lock` with double-checked locking to populate the in-memory store from SQLite exactly once on first access.
- **4 entry points updated**: `agent_1_adk/main.py`, `agent_2_llamaindex/server.py`, `agent_3_langgraph/server.py`, `agent_4_crewai/server.py` — `InMemoryTaskStore()` → `SQLiteTaskStore()`.
- **Schema migration**: `shared/memory/store.py` adds `a2a_tasks` table to `CREATE_TABLES_SQL`, bumps `SCHEMA_VERSION` to 3, and includes a migration block for existing databases.

### Memory Pruning / Retention Policy

- **`prune_old_records()` added** (`shared/memory/store.py`): Deletes rows older than `MEMORY_RETENTION_DAYS` (default 90) from `ticker_briefs`, `recommendation_records`, and `memory_entries`. Returns a dict of `{table: rows_deleted}`. Uses the existing `write_lock()` for concurrency safety.
- **Startup pruning** (`agent_1_adk/main.py`): Called on orchestrator startup — best-effort, wrapped in `try/except`. Logs deleted counts if any. Deliberately does not `VACUUM` to avoid blocking startup on large DB rewrites.
- **`.env.example` updated**: Documents `MEMORY_RETENTION_DAYS=90`.

### MCP Client Singleton with Auto-Reconnect

- **`get_shared_mcp()` added** (`shared/mcp_client.py`): Process-wide `MCPClient` singleton with double-checked async lock. Connects on first call; returns cached client on subsequent calls. Replaces per-request connect/disconnect in all executors, eliminating ~100–500ms SSE handshake overhead per request.
- **Auto-reconnect in `call_tool_by_name`**: On `ConnectionError`/`EOFError`/`asyncio.IncompleteReadError`, marks `_connected = False`, reconnects once, and retries the call. After 2 failures, raises. Prevents permanent MCP death on transient network blips.
- **`_connected` flag added**: Tracks connection state on `MCPClient`. Set `True` after `connect_all()`, `False` on reconnect attempts and in `disconnect_all()`.
- **`atexit` shutdown hook** (`_shutdown_mcp_sync`): Best-effort synchronous disconnect at process exit via a temporary event loop.
- **4 executors simplified**: `agent_1_adk/agent_executor.py`, `agent_2_llamaindex/executor.py`, `agent_3_langgraph/executor.py`, `agent_4_crewai/executor.py` — removed `_ensure_connected()`, `_disconnect()`, `finally` blocks in `stream()`, and temporary MCP creation. Now call `get_shared_mcp()` directly. Net: −131 lines across 4 files.

### Lazy OpenTelemetry Instrumentation

- **`init_instrumentation()` added** (`shared/observability.py`): New function that wraps all `*Instrumentor().instrument()` calls — each server calls it once at startup. A `_instrumented` set prevents double-instrumentation even if `init_instrumentation()` is called multiple times in the same process.
- **Deferred imports**: All OTel/OpenInference imports moved inside `init_instrumentation()` — importing a server module in pytest no longer triggers OTel side-effects (OTLP exporter threads, span processor startup).
- **5 entry points simplified**: `agent_1_adk/main.py`, `agent_1_adk/sub_agent_client.py`, `agent_2_llamaindex/server.py`, `agent_3_langgraph/server.py`, `agent_4_crewai/server.py`, `agents/finsight_agent/__init__.py` — replaced module-level `*Instrumentor().instrument()` with `init_instrumentation("<agent_type>")`.

### Correlation-ID Propagation via ContextVar

- **`current_trace_id` / `current_session_id` ContextVars** (`shared/trace_context.py`): New `contextvars.ContextVar` instances carrying the active trace and session IDs across async boundaries without explicit parameter passing.
- **`extract_trace_ids()` now sets ContextVar**: After parsing the trace prefix from an inbound task, automatically sets `current_trace_id` — any subsequent log line carries the ID without manual `extra=` passing.
- **`JsonFormatter` fallback to ContextVar**: The formatter checks `record.trace_id` first, then falls back to `current_trace_id.get()`. Same for `session_id`. This means log lines emitted by MCP tool handlers automatically include the caller's trace_id if one has been set.
- **`generic_executor` sets both ContextVars**: Before executing an inbound A2A task, extracts trace_id from the query and sets `current_trace_id` + `current_session_id` from the task's `context_id`.
- **`sub_agent_client` sets ContextVar**: Before injecting trace context into an outbound task, sets `current_trace_id` from the Langfuse current trace.
- **MCP tool log lines**: Hot-path tools (`get_prices`, `get_financials`, `get_company_filings`, `get_news_sentiment`) now emit `logger.info("Tool called", extra={"tool": "...", "ticker": "..."})` — the formatter automatically adds the active `trace_id` from ContextVar, so `grep <trace_id> logs/*.log` returns the full cross-service flow.

### Deduplicate Ticker Validation across Executors

- **`validate_ticker()` and `resolve_ticker()` added** (`shared/ticker_utils.py`): Module-level wrappers that use `get_shared_mcp()` singleton internally. Replace the copy-pasted `_validate_ticker`/`_resolve_ticker` private methods across all 4 executors.
- **~80 lines removed from executors**: `agent_1_adk/agent_executor.py`, `agent_2_llamaindex/executor.py`, `agent_3_langgraph/executor.py`, `agent_4_crewai/executor.py` — all private `_validate_ticker` and `_resolve_ticker` methods deleted. Each executor now calls the shared functions.
- **ADK executor inline MCP block replaced**: The input guardrail ticker pre-check in `agent_1_adk/agent_executor.py` (which previously created a temporary MCP client) now uses `validate_ticker()` from the shared singleton, eliminating the last ad-hoc MCP lifecycle in the codebase.

### Unified `@logged` Timing Decorator

- **`logged()` decorator added** (`shared/logging_config.py`): Emits `Enter` / `Exit` / `Fail` log lines with `latency_ms` as a structured JSON field. Uses `time.monotonic()` for precision and `fn.__qualname__` for consistent function identification.
- **Applied to sub-agent `_build_response()`**: `agent_2_llamaindex/executor.py`, `agent_3_langgraph/executor.py`, `agent_4_crewai/executor.py` — all three async generators wrapped. (Only `_build_response` is decorated, not `stream()`, because decorators on async generators would break the `yield` protocol.)
- **Applied to `SubAgentClient.send_message()`**: Captures latency for each outbound A2A call from the orchestrator to a sub-agent.
- **Applied to 4 MCP tool handlers** (`finsight_server.py`): `get_prices`, `get_financials`, `get_company_filings`, `get_news_sentiment` — every hot-path MCP call emits structured latency lines.
- **Value**: `grep "Exit" logs/*.log` shows per-call latencies across every service layer — orchestrator, sub-agents, MCP — in a single command.

### Cancellation Support + Per-Agent Timeouts

- **`GenericAgentExecutor.cancel()` implemented** (`shared/generic_executor.py`): Stores `asyncio.current_task()` on `execute()`, catches `asyncio.CancelledError` to emit `TASK_STATE_CANCELED` before re-raising. `cancel()` now calls `self._task.cancel()` instead of raising `NotImplementedError`.
- **`FinSightAgentExecutor.cancel()` implemented** (`agent_1_adk/agent_executor.py`): Same pattern — stores task, cancels on request. Replaces `NotImplementedError`.
- **Per-agent timeouts** (`agent_1_adk/sub_agent_client.py`): `send_message()` wraps the streaming loop in `asyncio.wait_for()` with per-agent timeouts (RAG=60s, Quant=90s, Sentiment=45s) derived from agent name; falls back to global `A2A_TIMEOUT` (180s). `TimeoutError` returns a clean `{"error": "agent_timeout", "agent": ..., "timeout": ...}` JSON payload instead of crashing.
- **`shared/config.py`**: Added `A2A_TIMEOUT_RAG`, `A2A_TIMEOUT_QUANT`, `A2A_TIMEOUT_SENTIMENT` env vars with sensible defaults.
- **Eval-trace write moved to `finally`**: In `send_message()`, the eval-trace write block moved from `try` body to `finally` — ensures traces are captured even on timeout.

## v1.24 — Before-Agent Cache Callback, IST Timezone & Stale Test Cleanup

### Before-Agent Cache Callback (`_memory_cache_callback`)

- **Two-tier same-day cache** (`agents/finsight_agent/agent.py`): New `_memory_cache_callback` registered as `root_agent.before_agent_callback` — fires before the LLM runs, extracts the user's ticker, queries `TickerMemory.get_latest()`, and returns today's cached brief (`types.Content`) directly if available. Short-circuits the LLM entirely, saving 30-60s per repeat same-day query.
- **Strict prompt directive** (`agent_1_adk/agent.py`): `[TODAY]` tag changed from "you MAY return it directly" to **"you MUST return it directly"** — reduces LLM variance on same-day cache hits.
- **Executor-level cache** (`agent_1_adk/agent_executor.py`): `_get_today_cached_text()` provides a parallel short-circuit for the A2A executor path, checking before `RUNNER.run_async()` is called.

### Response Text Overwrite for Cache Quality

- **`update_response_text()` added** (`shared/memory/ticker_memory.py`): Overwrites `brief_json.response_text` on an existing record after the agent turn completes. The `save_brief` tool's rationale is a short LLM-written summary; after the full synthesis finishes, the real analysis text replaces it — so the same-day cache returns the rich analysis, not the abbreviated rationale.
- **Integration in `_persist_memory_callback`** (`agents/finsight_agent/agent.py`): After memory persist, extracts the response text and calls `tm.update_response_text()`.

### IST Timezone Standardization

- **`IST` constant added** (`shared/config.py`): `IST = timezone(timedelta(hours=5, minutes=30))`. All `datetime.now()` calls across the system converted to use `IST` explicitly — agent timestamps, memory timestamps, analysis_date comparisons. Previously mixed between UTC and local machine time, causing same-day cache mismatches on non-IST systems.
- **Files changed**: `shared/config.py`, `shared/memory/memory_service.py`, `shared/memory/performance_tracker.py`, `shared/memory/portfolio_store.py`, `shared/memory/store.py`, `shared/memory/ticker_memory.py`, `agent_1_adk/agent.py`, `agent_1_adk/agent_executor.py`.

### Programmatic Dedup in save_brief & _store_memory

- **`save_brief` dedup** (`agent_1_adk/agent.py`): Checks if today's brief already exists for the ticker before inserting. Returns early with a confirmation message instead of creating a duplicate row.
- **`_store_memory` dedup** (`agent_1_adk/agent_executor.py`): Same check at the executor level — if `save_brief` already stored today's brief, `_store_memory` skips its own insert. Creates two-layer defense against duplicate records.

### Ticker Extraction: Dotted & Single-Char Tickers

- **Dotted tickers supported** (`shared/ticker_utils.py`): Patterns now match `[A-Z]{1,5}(?:\.[A-Z]{1,2})?` — handles Berkshire Hathaway (`BRK.A`, `BRK.B`) and other class-share tickers.
- **Single-char tickers**: Pattern 5 changed from `[A-Z]{2}` to `[A-Z]{1,2}`, enabling detection of tickers like `V` (Visa) and `Y` (Alleghany). New mixed-case parens pattern detects `V (Visa)` → `V`.
- **`$` prefix widened**: Pattern 3 (dollar prefix) now matches `[A-Z]{1,5}` instead of just `[A-Z]{1,2}`.

### _build_response Extracted from stream()

- **All three sub-agents refactored** (`agent_2_llamaindex/executor.py`, `agent_3_langgraph/executor.py`, `agent_4_crewai/executor.py`): The core response logic moved from inside `stream()` to a new `_build_response(query) → dict` method. `stream()` is now a thin wrapper: `yield await self._build_response(query)` in a `try/finally` block (ensuring `_disconnect()` runs). This makes the response-building logic independently testable and callable.

### DB Path Consolidation

- **Session DB separated** (`agent_1_adk/main.py`): ADK session store moved from `db/finsight_memory.db` to `db/adk_sessions.db` — separates conversational session data from ticker briefs and memory, preventing one schema migration from affecting the other.
- **User-id-agnostic cache queries**: `_get_today_cached_text()` and `_build_memory_context()` now query `TickerMemory.get_latest(ticker, user_id=None)` to avoid cache misses across different user_id values (a2a_user, user, eval_user).

### Stale Test Removal

- **17 test files removed**: All `tests/*.py` files and `tests/evaluation/` suite deleted — these were unmaintained fixtures from earlier architecture iterations that no longer matched the current codebase. Offline RAGAS evaluation pipeline, stale rubric tests, and outdated memory/integration tests all removed.
- **Test count: 0** — no automated test suite remains. Testing is performed manually via the ADK Web UI.

## v1.23 — Same-Day Memory Cache, analysis_date Column & Unified db/ Folder

### Same-Day Recommendation Cache

- **Date-aware memory injection** (`agent_1_adk/agent_executor.py`): `_build_memory_context()` now compares the stored brief's `analysis_date` against today's date. Context tagged `[TODAY]` when brief is from today (LLM may return directly without calling agents); tagged `[STALE]` when from a prior day (LLM must call all agents for fresh analysis).
- **Duplicate write prevention**: `_process_response()` skips the `_store_memory()` background task when `[TODAY]` is present in the injected user message, preventing identical records accumulating on same-day repeated queries.
- **Agent instruction updated** (`agent_1_adk/agent.py`): Both `_STATIC_PREAMBLE` and `_STATIC_PREAMBLE_FALLBACK` include a *MEMORY CONTEXT RULES* block instructing the LLM how to handle each tag.

### analysis_date Column

- **New `analysis_date TEXT` column** (`shared/memory/ticker_memory.py`, `shared/memory/store.py`): Added to `ticker_briefs` via idempotent `ALTER TABLE` migration in `init_db()`. Both `store_brief()` and `store_minimal()` write `date.today().isoformat()` into this column on every insert.
- **Read path updated**: `get_latest()` and `get_history()` select and return `analysis_date` as `row[9]`. Sort order changed to `ORDER BY COALESCE(analysis_date, created_at) DESC` — uses explicit date where available, falls back to `created_at` for legacy rows.

### Unified db/ Folder

- **All databases consolidated under `db/`**: `shared/memory/store.py` `DB_PATH` → `db/finsight_memory.db`. Session DB URL in `agent_1_adk/main.py` → `sqlite+aiosqlite:///./db/finsight_memory.db`. LangChain cache in `agent_3_langgraph/nodes.py` → `db/.langchain_cache.db`. ChromaDB default in `shared/config.py` (`CHROMA_DIR`) → `./db/chroma_db`.
- **`.gitignore` simplified**: All scattered per-file DB ignore entries replaced with a single `db/` rule. Removed duplicate entries and stale repeated lines.
- `db/` directory auto-created on first run via `path.parent.mkdir(parents=True, exist_ok=True)` in `get_db()`.

## v1.22 — ADK 2.x, Eval Toggle, Memory Pollution Fix & Score Namespacing

### Google ADK 2.x Upgrade

- **`google-adk` bumped to `>=2.0,<3.0`** (`pyproject.toml`): installed `2.1.0`. No code changes required — all public API surfaces (`LlmAgent`, `Runner`, `DatabaseSessionService`, `BaseMemoryService`, `google.adk.tools.{google_search, load_memory}`, `google.adk.cli.service_registry.get_service_registry`) verified stable. Custom `SQLiteMemoryService` still satisfies the 2.x `BaseMemoryService` signatures. Project's `BaseAgent` in `shared/base_agent.py` is unaffected (it is a Pydantic class, not ADK's `BaseAgent`).

### `EVAL_TRACE_ENABLED` Feature Flag

- **`EVAL_ENABLED` constant added** (`shared/config.py`): reads `EVAL_TRACE_ENABLED` from `.env` (default `True`). Single source of truth for whether sidecar RAGAS evals fire.
- **All `asyncio.create_task(_eval_*)` calls gated**: every agent now checks `if EVAL_ENABLED:` before scheduling its eval task. Sites: `agent_1_adk/agent_executor.py`, `agents/finsight_agent/agent.py` (orchestrator), `agent_2_llamaindex/executor.py` (RAG), `agent_3_langgraph/executor.py` (quant), `agent_4_crewai/executor.py` (sentiment). Set `EVAL_TRACE_ENABLED=False` in `.env` to disable all sidecar evals with no code changes.

### Orchestrator Eval Moved to `after_agent_callback`

- **Problem**: when running via `adk web`, the orchestrator goes through ADK's built-in runner — `FinSightAgentExecutor` is never invoked. The eval call in `agent_executor.py` only fired for A2A clients hitting `agent_1_adk/main.py`. With the orchestrator A2A server removed from the bat file, evals stopped firing entirely.
- **Fix** (`agents/finsight_agent/agent.py`): added orchestrator eval scheduling into the existing `_persist_memory_callback`. After memory persist, extracts user query + final agent text from `session.events`, pulls current Langfuse `trace_id`, and fires `asyncio.create_task(_eval_score_response(...))`. Works for both `adk web` and any other ADK runner path.

### Memory Persist + Eval Gated on `save_brief`

- **Problem**: `_persist_memory_callback` fired on every agent turn — including pure recall turns where the user asked "what were my last recommendations?". That conversational exchange was being indexed into long-term memory and evaluated, polluting future memory searches and inflating eval volume.
- **Fix** (`agents/finsight_agent/agent.py`): added `_is_analysis_turn()` which walks back to the most recent user message and checks whether `save_brief` was called after it. If not, both memory persist and eval are skipped. Logs `"Skipping persist + eval — turn did not call save_brief"` for visibility.
- Behaviour: "Analyze AAPL" → `save_brief` called → persists + evals. "What were my last recommendations?" → only `load_memory` → skipped. "Show me last NVDA brief, then analyze TSLA" → `save_brief` called for TSLA → persists.

### Langfuse Score Namespacing by Agent

- **`_push_scores` now prefixes scores by agent** (`shared/runtime_eval.py`): `ragas/{name}` → `ragas/{agent}/{name}` (e.g. `ragas/orchestrator/AnswerRelevancy`, `ragas/rag/Faithfulness`). The previous flat namespace made it impossible to distinguish "the orchestrator's AnswerRelevancy" from "RAG's AnswerRelevancy" in Langfuse.
- **`comment="agent=<name>"` added** to each `lf.create_score()` call for an additional structured tag.

### `RubricsScoreWithoutReference` Import Fix

- **Problem**: `score_response()` orchestrator eval imported `RubricsScoreWithoutReference` from `ragas.metrics.collections` — that class does not exist in ragas 0.4.x. The import failed and the entire orchestrator eval bailed out with `[orchestrator] Skipping eval: ragas import failed`.
- **Fix** (`shared/runtime_eval.py`): `recommendation_clarity` metric now uses `DomainSpecificRubrics` (the actual reference-free rubric class in 0.4.x). Same scoring rubric, working import.

### Removed Duplicate Batch-Eval Runner

- **Deleted from `shared/runtime_eval.py`**: `_invoke_agent()`, `_run_batch_eval()`, `_BATCH_EVAL_CASES`, and the `if __name__ == "__main__":` block. `_invoke_agent` spun up its own `Runner` + `InMemorySessionService` to invoke the orchestrator — duplicating exactly what `FinSightAgentExecutor` and `after_agent_callback` already do for live traffic. The live executor has the response in hand; no second runner needed.
- Batch evaluation with ground-truth references still lives in `tests/evaluation/run_orchestrator_eval.py`.

### Bat-File Cleanup

- **Orchestrator A2A server removed from `run_adk_web.bat`**: `agent_1_adk/main.py` is no longer started. The orchestrator runs through `adk web` on port `8080`. The A2A endpoint at `:8001` is no longer exposed by default; bring it back manually with `uv run python -m agent_1_adk.main` if needed for A2A clients.
- `stop_servers.bat` already kills the port; PowerShell terminal-close command targets all `uv run` and `lms server` windows reliably.

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
- **LangChain SQLiteCache** (`agent_3_langgraph/nodes.py`): `SQLiteCache(database_path="db/.langchain_cache.db")` wraps the quant agent's LLM summary call — identical ticker+metrics inputs reuse the cached LLM response. Requires `langchain-community>=0.3.0`.
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

- **`DatabaseSessionService` replaces `InMemorySessionService`**: ADK's built-in `DatabaseSessionService` with `sqlite+aiosqlite:///./db/finsight_memory.db` provides persistent session/event storage across restarts. Full conversation history (user messages, agent responses, tool calls) is saved to SQLite.
- **`SQLiteMemoryService` for cross-session memory search**: Custom implementation of ADK's `BaseMemoryService` that persists conversation events to SQLite. The `load_memory` tool can search past conversations across sessions and restarts. Sessions are auto-ingested after each successful response.
- **`TickerMemory` for structured brief history**: Stores per-ticker investment recommendations with ticker, recommendation (BUY/HOLD/SELL), confidence, full response text, and timestamp. Provides `format_context()` that generates a compact (~300 token) memory summary injected into the orchestrator's system prompt before each query.
- **`PortfolioStore` for user profile persistence**: Auto-captures portfolio holdings from each query's context. Merges holdings over time — users never need to explicitly set their portfolio. Stores risk profile and investment horizon.
- **`PerformanceTracker` for recommendation outcomes**: Records each BUY/HOLD/SELL recommendation with optional price snapshot. Can evaluate past recommendations against current market prices via yfinance. Provides accuracy stats (win rate by recommendation type).
- **Memory context injection**: Before each query, the executor extracts the ticker, retrieves the latest recommendation from `TickerMemory`, and prepends it to the user message. This enables the LLM to answer "Has the outlook for NVDA changed since last time?"
- **Auto-save on every response**: `agent_executor.py` automatically stores briefs, recommendations, and portfolio updates after every successful response — no LLM action required.
- **`save_brief` tool removed**: Simplified to auto-save only. The LLM no longer needs to explicitly call a tool to persist its analysis.
- **`load_memory` tool added to orchestrator**: The ADK `load_memory` tool is now available to the orchestrator LLM for searching past conversations.
- **`db/` folder added to `.gitignore`**: All database files (`finsight_memory.db`, `chroma_db/`, `.langchain_cache.db`) consolidated under `db/` and excluded via a single rule.
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
