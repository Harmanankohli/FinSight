# FinSight — Implementation Plan

Detailed, step-by-step plan for each improvement. Ordered low → high effort.
Each activity is self-contained: goal, files, steps, verification, rollback.

**Scope filter applied**: items rated "overengineered for project scale" in the
prior assessment are listed at the bottom with a "skip" note and rationale —
they are not detailed here. Re-include them later only if usage patterns
justify the cost.

**Test status**: all prior tests were deleted by the user. The test plan below
starts from a clean slate and is sized for this project (not the 280-test
production-grade plan in `TEST_PLAN.md`).

---

## Index

**Tier 0 — Trivial (~10–30 min each)**
1. [SEC EDGAR User-Agent env var](#1-sec-edgar-user-agent-env-var)
2. [Hardcoded `api_key="lmstudio"` → env var](#2-hardcoded-api_keylmstudio--env-var)

**Tier 1 — Small (~1–2 hrs each)**
3. [SQLite long-lived connection + write lock](#3-sqlite-long-lived-connection--write-lock)
4. [Token-bucket rate limiter (SEC + yfinance + RSS)](#4-token-bucket-rate-limiter)
5. [yfinance / news TTL cache](#5-yfinance--news-ttl-cache)
6. [Structured JSON logging](#6-structured-json-logging)
7. [Per-service log levels via env](#7-per-service-log-levels-via-env)
8. [Log sanitization filter](#8-log-sanitization-filter)
9. [`SQLiteTaskStore` replacing `InMemoryTaskStore`](#9-sqlitetaskstore)
10. [Memory pruning / retention policy](#10-memory-pruning--retention-policy)

**Tier 2 — Medium (~½ day each)**
11. [MCP client singleton with auto-reconnect](#11-mcp-client-singleton-with-auto-reconnect)
12. [Lazy OpenTelemetry instrumentation](#12-lazy-opentelemetry-instrumentation)
13. [Correlation-ID propagation via ContextVar](#13-correlation-id-propagation)
14. [Deduplicate ticker validation across executors](#14-deduplicate-ticker-validation)
15. [Unified `@logged` timing decorator](#15-unified-logged-timing-decorator)
16. [Cancellation support + per-agent timeout](#16-cancellation-support)

**Tier 3 — Larger**
17. [Test suite — pragmatic starter set](#17-test-suite--pragmatic-starter-set)
18. [Security sandbox hardening + tests](#18-security-sandbox-hardening--tests)

**Skipped (with rationale)** — see [bottom of file](#skipped-with-rationale).

---

## 1. SEC EDGAR User-Agent env var

**Goal**: Stop SEC blocking the system due to non-compliant `contact@finsight.com`
placeholder. SEC enforces real contact emails in User-Agent headers.

**Files to touch**
- `shared/config.py`
- `mcp_servers/finsight_server.py:318` (definition of `_SEC_HEADERS`)
- `.env.example` (or create one)

**Steps**

1. Add to `shared/config.py`, near the SEC section (line ~61):
   ```python
   SEC_USER_AGENT = os.environ.get(
       "SEC_USER_AGENT",
       "FinSight Research (dev-mode-set-SEC_USER_AGENT)",
   )
   ```
2. In `shared/config.py.validate()`, add a warning when the placeholder is in
   use:
   ```python
   if "dev-mode" in SEC_USER_AGENT:
       logging.getLogger(__name__).warning(
           "SEC_USER_AGENT is placeholder — SEC may rate-limit or block. "
           "Set SEC_USER_AGENT='Your Name (your-email@example.com)' in .env"
       )
   ```
3. In `mcp_servers/finsight_server.py`, replace lines 318–320:
   ```python
   from shared.config import SEC_USER_AGENT
   _SEC_HEADERS = {"User-Agent": SEC_USER_AGENT}
   ```
4. Add `SEC_USER_AGENT=` to `.env.example` with a comment about format.

**Verification**
- Start MCP server with `SEC_USER_AGENT` unset → warning appears in logs.
- Start with `SEC_USER_AGENT="Test User (test@example.com)"` → no warning, header
  is set correctly (verify with a SEC tool call and inspecting request headers
  in logs).

**Rollback**: revert the 3 edits; no schema or state changes.

---

## 2. Hardcoded `api_key="lmstudio"` → env var

**Goal**: Single source of truth for LLM API key. Also unblocks pointing the
system at OpenAI/Anthropic without code edits (it's not really a "secret leak"
issue — `lmstudio` is the LM Studio dummy value — but the maintenance cost is
real).

**Files to touch**
- `shared/config.py`
- `agent_2_llamaindex/index_manager.py:28`
- `agent_3_langgraph/nodes.py:439`
- `agent_4_crewai/crew.py:15`

**Steps**

1. In `shared/config.py`, near LLM section:
   ```python
   LLM_API_KEY = os.environ.get("LLM_API_KEY", "lmstudio")
   ```
2. In each of the 3 files:
   - Add `from shared.config import LLM_API_KEY` (or extend existing import).
   - Replace `api_key="lmstudio"` with `api_key=LLM_API_KEY`.
3. Add `LLM_API_KEY=lmstudio` to `.env.example` with a comment that it's a
   placeholder for LM Studio's local server.

**Verification**
- `python -c "from shared.config import LLM_API_KEY; print(LLM_API_KEY)"` →
  prints "lmstudio" by default, or the env-overridden value.
- Boot agents → no behavior change with default. Set `LLM_API_KEY=test-key` →
  visible in agent boot logs if you log it (don't — add to sanitization filter
  in #8).

**Rollback**: revert 4 edits.

---

## 3. SQLite long-lived connection + write lock

**Goal**: Eliminate the open-close-per-call pattern in `shared/memory/`.
Reduces lock contention; small but clean win.

**Files to touch**
- `shared/memory/store.py` (the core change)
- `shared/memory/ticker_memory.py` (remove 4 `await conn.close()` calls at lines 47, 83, 125, 167)
- `shared/memory/portfolio_store.py` (remove 3 closes at lines 40, 82, 111)
- `shared/memory/performance_tracker.py` (remove 5 closes at lines 57, 74, 96, 137, 205)
- `shared/memory/memory_service.py` (remove 3 closes at lines 81, 137, 224)
- `shared/memory/store.py` (remove its own closes at lines 137, 151)

**Steps**

1. In `shared/memory/store.py`, replace `get_db()` with a singleton:
   ```python
   import asyncio

   _db_conn: aiosqlite.Connection | None = None
   _db_lock = asyncio.Lock()
   _init_lock = asyncio.Lock()

   async def get_db(path: Path = DB_PATH) -> aiosqlite.Connection:
       global _db_conn
       if _db_conn is not None:
           return _db_conn
       async with _init_lock:
           if _db_conn is not None:
               return _db_conn
           path.parent.mkdir(parents=True, exist_ok=True)
           conn = await aiosqlite.connect(str(path))
           await conn.execute("PRAGMA journal_mode=WAL")
           await conn.execute("PRAGMA foreign_keys=ON")
           await conn.execute("PRAGMA busy_timeout=5000")
           await init_db(conn)
           _db_conn = conn
       return _db_conn

   async def close_db() -> None:
       """Optional: call at process shutdown."""
       global _db_conn
       if _db_conn is not None:
           await _db_conn.close()
           _db_conn = None
   ```
2. Add a `write_lock()` helper for callers performing writes:
   ```python
   def write_lock() -> asyncio.Lock:
       return _db_lock
   ```
3. In every writer (e.g. `ticker_memory.store_brief`), wrap the write block:
   ```python
   from shared.memory.store import get_db, write_lock

   async def store_brief(self, ...):
       async with write_lock():
           conn = await get_db(self._db_path)
           await conn.execute(...)
           await conn.commit()
   ```
4. Remove all `await conn.close()` calls (the 17 locations grep'd above).
5. Reads do **not** need the lock; just `conn = await get_db(...)` then query.
6. Register `close_db` at process shutdown in each server entry point
   (`main.py` / `server.py`) via `atexit` or FastAPI lifespan.

**Verification**
- `tests/test_memory_concurrency.py` (new, ~30 lines): spawn 20 concurrent
  `store_brief` calls with different tickers, assert all rows present.
- Manually: `sqlite3 finsight_memory.db "SELECT count(*) FROM ticker_briefs"`
  before/after a multi-query orchestrator run.

**Risks**
- A single connection serializes everything. If you ever introduce long-running
  read transactions, they'll block writes. Mitigation: keep writes short
  (current writes are already single-statement).
- Forgetting to wrap a writer with `write_lock()` is a silent race. Mitigation:
  grep for `INSERT|UPDATE|DELETE` in `shared/memory/` and confirm each is inside
  a `write_lock()` block.

**Rollback**: revert `store.py` + restore `await conn.close()` calls in 4 modules.

---

## 4. Token-bucket rate limiter

**Goal**: Stop SEC IP bans and Yahoo 429s by enforcing a soft request ceiling.

**Files to touch**
- `shared/rate_limiter.py` (new, ~40 lines)
- `mcp_servers/finsight_server.py` (apply to 3 hot paths)

**Steps**

1. Create `shared/rate_limiter.py`:
   ```python
   import asyncio
   import time

   class TokenBucket:
       def __init__(self, rate: float, burst: int):
           self.rate = rate
           self.burst = burst
           self.tokens = float(burst)
           self.last = time.monotonic()
           self._lock = asyncio.Lock()

       async def acquire(self) -> None:
           while True:
               async with self._lock:
                   now = time.monotonic()
                   self.tokens = min(
                       self.burst,
                       self.tokens + (now - self.last) * self.rate,
                   )
                   self.last = now
                   if self.tokens >= 1:
                       self.tokens -= 1
                       return
                   deficit = 1 - self.tokens
               await asyncio.sleep(deficit / self.rate)
   ```
2. In `mcp_servers/finsight_server.py`, add module-level limiters:
   ```python
   from shared.rate_limiter import TokenBucket
   _sec_limiter = TokenBucket(rate=8, burst=10)      # SEC: 10 req/s hard limit
   _yfinance_limiter = TokenBucket(rate=4, burst=8)
   _rss_limiter = TokenBucket(rate=2, burst=4)
   ```
3. Before each SEC HTTP call (search for `_SEC_HEADERS` usage):
   ```python
   await _sec_limiter.acquire()
   resp = await client.get(...)
   ```
4. Before each yfinance call (lines 272–280 area, `get_prices`):
   ```python
   await _yfinance_limiter.acquire()
   ```
5. Before each RSS fetch in `_fetch_rss` (line ~1152):
   ```python
   await _rss_limiter.acquire()
   ```

**Verification**
- Spawn 50 concurrent `get_prices` calls in a script; observe wall time ≈
  `50 / rate` seconds rather than all firing instantly.
- Tail logs while running a full orchestrator query → no 429 / 403 from
  Yahoo or SEC.

**Risks**
- Too restrictive limiter blocks legitimate concurrent users. Mitigation:
  pick rates conservatively (SEC's stated cap is 10/s, leave headroom).
- TokenBucket recursion in original doc is a stack-blowup risk under
  contention; the loop form above avoids that.

**Rollback**: delete the `acquire()` calls; rate limiter file can remain unused.

---

## 5. yfinance / news TTL cache

**Goal**: Same ticker requested 3× in one orchestrator turn → 1 actual network
call. Biggest perf/effort ratio in the whole plan.

**Files to touch**
- `shared/ttl_cache.py` (new, ~40 lines)
- `mcp_servers/finsight_server.py` (apply to `get_prices`, `get_financials`,
  `_fetch_rss`, `get_news_sentiment`)

**Steps**

1. Create `shared/ttl_cache.py`:
   ```python
   import asyncio
   import time
   from typing import Any, Callable, Awaitable

   class TTLCache:
       def __init__(self, ttl_seconds: float, max_entries: int = 512):
           self.ttl = ttl_seconds
           self.max_entries = max_entries
           self._data: dict[str, tuple[float, Any]] = {}
           self._lock = asyncio.Lock()
           self._inflight: dict[str, asyncio.Future] = {}

       async def get_or_fetch(
           self, key: str, fetch: Callable[[], Awaitable[Any]]
       ) -> Any:
           now = time.monotonic()
           entry = self._data.get(key)
           if entry and now - entry[0] < self.ttl:
               return entry[1]

           # Single-flight: collapse concurrent misses into one fetch.
           async with self._lock:
               if key in self._inflight:
                   fut = self._inflight[key]
               else:
                   fut = asyncio.get_event_loop().create_future()
                   self._inflight[key] = fut
                   asyncio.create_task(self._fetch_and_store(key, fetch, fut))
           return await fut

       async def _fetch_and_store(self, key, fetch, fut):
           try:
               value = await fetch()
               self._data[key] = (time.monotonic(), value)
               if len(self._data) > self.max_entries:
                   # Drop oldest (cheap approximation: pop arbitrary).
                   self._data.pop(next(iter(self._data)))
               fut.set_result(value)
           except Exception as exc:
               fut.set_exception(exc)
           finally:
               self._inflight.pop(key, None)
   ```
2. In `mcp_servers/finsight_server.py`:
   ```python
   from shared.ttl_cache import TTLCache
   _prices_cache = TTLCache(ttl_seconds=60)          # 1 min — intraday OK
   _financials_cache = TTLCache(ttl_seconds=3600)    # 1 hr — quarterly data
   _news_cache = TTLCache(ttl_seconds=300)           # 5 min
   ```
3. Wrap each hot tool. Example for `get_prices`:
   ```python
   @mcp.tool()
   async def get_prices(ticker: str, period: str = "1y", ...) -> dict:
       key = f"prices:{ticker}:{period}:{interval}"
       return await _prices_cache.get_or_fetch(
           key, lambda: _get_prices_uncached(ticker, period, interval),
       )
   ```
   Rename the current body to `_get_prices_uncached`.
4. Repeat for `get_financials`, `get_news_sentiment`.
5. **Do not cache** `validate_ticker` or `execute_python` — both have
   correctness or security implications.

**Verification**
- Two consecutive `get_prices("AAPL")` calls → 2nd returns in <5ms vs >200ms.
- 20 concurrent identical calls → only one shows up in yfinance logs.
- Wait 65s; call again → fresh fetch (TTL expiry works).

**Risks**
- Stale data during a fast-moving market event. Mitigation: keep TTL short for
  prices (60s) and add a `force_refresh` flag if needed later.
- Memory grows unbounded if `max_entries` accidentally large. Default 512 is
  fine for normal use.

**Rollback**: delete the cache calls; uncached functions still work.

---

## 6. Structured JSON logging

**Goal**: Make logs ingestible by Loki/CloudWatch/Datadog without custom
parsers. Foundation for #13 (correlation IDs).

**Files to touch**
- `shared/logging_config.py` only.

**Steps**

1. Add a JSON formatter class at the top of `shared/logging_config.py`:
   ```python
   import json
   from datetime import datetime, timezone

   class JsonFormatter(logging.Formatter):
       def __init__(self, service_name: str):
           super().__init__()
           self.service_name = service_name

       def format(self, record: logging.LogRecord) -> str:
           payload = {
               "ts": datetime.now(timezone.utc).isoformat(),
               "level": record.levelname,
               "service": self.service_name,
               "logger": record.name,
               "message": record.getMessage(),
           }
           # Pick up structured extras (used by #13 correlation IDs).
           for k in ("trace_id", "session_id", "ticker", "latency_ms"):
               val = getattr(record, k, None)
               if val is not None:
                   payload[k] = val
           if record.exc_info:
               payload["exc"] = self.formatException(record.exc_info)
           return json.dumps(payload, default=str)
   ```
2. Update `setup_file_logging`: pass `service_name` to `JsonFormatter` for the
   file handler. Keep the existing plaintext formatter for the StreamHandler so
   terminal output stays readable.
3. Test by running an agent and inspecting `logs/<service>.log` — each line
   should be valid JSON parseable by `jq`.

**Verification**
```
tail -1 logs/orchestrator.log | python -m json.tool
```
Should print formatted JSON, not error.

**Rollback**: swap the formatter back to the old `logging.Formatter`.

---

## 7. Per-service log levels via env

**Goal**: Crank up MCP server debugging without flooding orchestrator logs.

**Files to touch**
- `shared/logging_config.py`
- `.env.example`

**Steps**

1. In `setup_file_logging`, replace `level: int = logging.INFO` with env lookup:
   ```python
   def setup_file_logging(service_name: str, level: int | None = None) -> None:
       if level is None:
           env_key = f"LOG_LEVEL_{service_name.upper().replace('-', '_')}"
           level_str = os.environ.get(env_key) or os.environ.get("LOG_LEVEL", "INFO")
           level = getattr(logging, level_str.upper(), logging.INFO)
       ...
   ```
2. Document in `.env.example`:
   ```
   # Global default
   LOG_LEVEL=INFO
   # Per-service overrides (uppercase service name)
   # LOG_LEVEL_ORCHESTRATOR=DEBUG
   # LOG_LEVEL_MCP=DEBUG
   # LOG_LEVEL_QUANT=WARNING
   ```

**Verification**
- `LOG_LEVEL_MCP=DEBUG` then start MCP server → DEBUG lines appear in
  `logs/mcp.log` only; other services unchanged.

**Rollback**: hardcode `level=logging.INFO` again.

---

## 8. Log sanitization filter

**Goal**: Defense in depth — scrub known secret patterns before write.

**Files to touch**
- `shared/logging_config.py`

**Steps**

1. Add at top of `shared/logging_config.py`:
   ```python
   import re

   class SanitizeFilter(logging.Filter):
       _PATTERNS = [
           (re.compile(r"(api_key\s*=\s*)['\"]?[^'\"\s,)]+['\"]?"), r"\1***"),
           (re.compile(r"sk-[A-Za-z0-9]{20,}"), "sk-***"),
           (re.compile(r"pk-[A-Za-z0-9]{20,}"), "pk-***"),
           (re.compile(r"(Authorization:\s*Bearer\s+)\S+"), r"\1***"),
           (re.compile(r"(LANGFUSE_(?:PUBLIC|SECRET)_KEY\s*[=:]\s*)\S+"), r"\1***"),
       ]

       def filter(self, record: logging.LogRecord) -> bool:
           if isinstance(record.msg, str):
               for pat, repl in self._PATTERNS:
                   record.msg = pat.sub(repl, record.msg)
           if record.args:
               new_args = []
               for a in record.args:
                   if isinstance(a, str):
                       for pat, repl in self._PATTERNS:
                           a = pat.sub(repl, a)
                   new_args.append(a)
               record.args = tuple(new_args)
           return True
   ```
2. In `setup_file_logging`, attach to both handlers:
   ```python
   sanitize = SanitizeFilter()
   sh.addFilter(sanitize)
   fh.addFilter(sanitize)
   ```

**Verification**
- `logger.info("user api_key=sk-secret-real-key-here")` → log line shows
  `api_key=***`.

**Rollback**: detach the filter.

---

## 9. `SQLiteTaskStore`

**Goal**: Sub-agent A2A tasks survive restart instead of vanishing.

**Files to touch**
- `shared/a2a_store.py` (new)
- `agent_1_adk/main.py:75`
- `agent_2_llamaindex/server.py:85`
- `agent_3_langgraph/server.py:78`
- `agent_4_crewai/server.py:74`

**Steps**

1. Read the `a2a.server.tasks.TaskStore` protocol (just open one in venv site-packages
   to see the abstract methods). At minimum: `save`, `get`, `delete`.
2. Create `shared/a2a_store.py` implementing each method against a single
   SQLite table `a2a_tasks(task_id TEXT PRIMARY KEY, payload TEXT, updated_at TS)`.
   Use the same `get_db()` from #3 (after #3 lands).
3. Add table creation to `CREATE_TABLES_SQL` in `shared/memory/store.py` and bump
   `SCHEMA_VERSION` to 3.
4. In each server entry point, swap:
   ```python
   - task_store = InMemoryTaskStore()
   + from shared.a2a_store import SQLiteTaskStore
   + task_store = SQLiteTaskStore()
   ```

**Verification**
- Start agent, send a long-running A2A task, kill the process, restart, query
  task status → returns the in-progress task instead of "not found".

**Risks**
- SQLite write latency now on the A2A hot path. Mitigation: depends on #3
  already being in place.

**Rollback**: swap back to `InMemoryTaskStore` in the 4 entry points; the
table sticks around harmlessly.

---

## 10. Memory pruning / retention policy

**Goal**: `ticker_briefs` and `recommendation_records` grow unbounded.
Cap retention so the DB doesn't bloat over months.

**Files to touch**
- `shared/memory/store.py` (add a `prune()` function)
- `agent_1_adk/main.py` (call at startup or schedule)

**Steps**

1. In `shared/memory/store.py`:
   ```python
   from datetime import datetime, timedelta

   async def prune_old_records(days: int = 90) -> dict[str, int]:
       cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
       conn = await get_db()
       async with write_lock():
           deleted = {}
           for table in ("ticker_briefs", "recommendation_records", "memory_entries"):
               cur = await conn.execute(
                   f"DELETE FROM {table} WHERE created_at < ?", (cutoff,),
               )
               deleted[table] = cur.rowcount
           await conn.execute("VACUUM")
           await conn.commit()
       return deleted
   ```
2. Expose retention window via env: `MEMORY_RETENTION_DAYS=90`.
3. Call `prune_old_records()` once on orchestrator startup (best-effort,
   wrapped in try/except).

**Verification**
- Insert a row with `created_at` 100 days ago, run `prune_old_records(90)`,
  confirm row deleted.

**Risks**
- `VACUUM` rewrites the entire file. Don't run it on every startup — gate it
  behind a "last vacuum > 7 days ago" check, or call it manually.

**Rollback**: stop calling `prune_old_records`; data stays.

---

## 11. MCP client singleton with auto-reconnect

**Goal**: Eliminate per-request SSE connect/disconnect (~100–500ms) AND avoid
the "fragile singleton" failure mode (connection drops kill the whole agent
until restart).

**Files to touch**
- `shared/mcp_client.py` (add singleton helpers, add reconnect logic)
- `agent_1_adk/agent_executor.py` (line ~108: drop local MCPClient construction)
- `agent_2_llamaindex/executor.py` (line ~108: same)
- `agent_3_langgraph/executor.py` (line ~34: same)
- `agent_4_crewai/executor.py` (line ~27: same)

**Steps**

1. In `shared/mcp_client.py`, add at module level:
   ```python
   _global_client: MCPClient | None = None
   _client_lock = asyncio.Lock()

   async def get_shared_mcp() -> MCPClient:
       global _global_client
       if _global_client is not None and _global_client._connected:
           return _global_client
       async with _client_lock:
           if _global_client is None or not _global_client._connected:
               from shared.config import MCP_SERVER_URL, MCP_TIMEOUT
               _global_client = MCPClient(
                   configs=[MCPServerConfig(name="finsight-mcp", url=MCP_SERVER_URL)],
                   timeout=MCP_TIMEOUT,
               )
               await _global_client.connect_all()
       return _global_client
   ```
2. Add a `_connected: bool` attribute to `MCPClient.__init__`, set True after
   `connect_all` succeeds, False inside any catch block where the SSE stream
   dies.
3. Wrap `call_tool_by_name` so that on `ConnectionError`/`ClosedResourceError`/
   `asyncio.IncompleteReadError`, it sets `_connected = False`, reconnects
   once, and retries the call. After 2 failures, raise.
4. In each of the 4 executors:
   - Remove the constructor's `self._mcp = MCPClient(...)` line.
   - Replace `self._mcp` usages with `await get_shared_mcp()`.
   - Delete `_ensure_connected` and `_disconnect` methods.
   - Remove the `await self._disconnect()` cleanup blocks.
5. Add a process-shutdown hook (atexit or lifespan) that calls
   `await _global_client.disconnect_all()` if non-None.

**Verification**
- Start orchestrator + sub-agents. Run a query, watch MCP server logs → one
  SSE connection per agent, no churn.
- Kill MCP server mid-query, restart it within 5s → next query succeeds
  (reconnect worked).
- Run 10 sequential queries → no leaked connections (`netstat` should show 5
  total SSE connections — one per agent).

**Risks**
- Auto-reconnect with stale auth/credentials can loop. Mitigation: the cap of
  2 retries before raising.
- Concurrent first-call: handled by `_client_lock` double-checked pattern.

**Rollback**: restore per-executor `MCPClient` construction; delete the
singleton helpers.

---

## 12. Lazy OpenTelemetry instrumentation

**Goal**: Stop fire-on-import behaviour so `pytest` can load modules without
hitting OTel side effects. Prerequisite for #17.

**Files to touch**
- `shared/observability.py` (add `init_instrumentation`)
- `agent_1_adk/sub_agent_client.py:12` (remove top-level `HTTPXClientInstrumentor().instrument()`)
- `agent_2_llamaindex/server.py:26,29` (remove `LlamaIndexInstrumentor().instrument()` + `StarletteInstrumentor().instrument()`)
- `agent_3_langgraph/server.py:26,30` (remove `StarletteInstrumentor().instrument()` + `LangChainInstrumentor().instrument()`)
- `agent_4_crewai/server.py:26,29` (remove `CrewAIInstrumentor().instrument()` + `StarletteInstrumentor().instrument()`)
- Each server's `main()` / startup hook to call `init_instrumentation(...)`

**Steps**

1. In `shared/observability.py` add:
   ```python
   _instrumented: set[str] = set()

   def init_instrumentation(agent_type: str) -> None:
       if agent_type in _instrumented:
           return
       _instrumented.add(agent_type)
       if agent_type == "orchestrator":
           from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
           HTTPXClientInstrumentor().instrument()
       elif agent_type == "rag":
           from openinference.instrumentation.llama_index import LlamaIndexInstrumentor
           from opentelemetry.instrumentation.starlette import StarletteInstrumentor
           LlamaIndexInstrumentor().instrument()
           StarletteInstrumentor().instrument()
       elif agent_type == "quant":
           from opentelemetry.instrumentation.starlette import StarletteInstrumentor
           StarletteInstrumentor().instrument()
           try:
               from openinference.instrumentation.langchain import LangChainInstrumentor
               LangChainInstrumentor().instrument()
           except ImportError:
               pass
       elif agent_type == "sentiment":
           from openinference.instrumentation.crewai import CrewAIInstrumentor
           from opentelemetry.instrumentation.starlette import StarletteInstrumentor
           CrewAIInstrumentor().instrument(skip_dep_check=True)
           StarletteInstrumentor().instrument()
   ```
2. In each server's `main()` (or the Starlette app factory `get_app()`), call
   `init_instrumentation("rag")` (or matching agent type) BEFORE constructing
   the app.
3. In `agent_1_adk/sub_agent_client.py`, move the instrumentation call into the
   `SubAgentClient.__init__` or the orchestrator's startup, gated on a
   one-shot flag.
4. Remove all module-level `*Instrumentor().instrument()` calls.

**Verification**
- `python -c "import agent_2_llamaindex.server"` → no Langfuse / OTel spans
  emitted (used to happen at import).
- Boot agents normally → Langfuse dashboard still shows full traces.

**Risks**
- Forget to call `init_instrumentation` in one server → that agent silently
  loses OTel coverage. Mitigation: assertion or warning if the corresponding
  Langfuse spans don't appear; covered by your existing eval traces.

**Rollback**: restore the top-level `*.instrument()` calls; remove
`init_instrumentation` calls from main().

---

## 13. Correlation-ID propagation

**Goal**: One `grep <trace_id> logs/*.log` returns the entire cross-service flow.

**Files to touch**
- `shared/trace_context.py` (add ContextVar)
- `shared/logging_config.py` (read ContextVar in formatter — already plumbed by #6)
- `agent_1_adk/sub_agent_client.py` (set ContextVar when injecting trace)
- `shared/generic_executor.py` (set ContextVar when extracting trace from inbound task)
- `mcp_servers/finsight_server.py` (read+log incoming trace_id on each tool call)

**Steps**

1. In `shared/trace_context.py` add:
   ```python
   import contextvars

   current_trace_id: contextvars.ContextVar[str | None] = \
       contextvars.ContextVar("trace_id", default=None)
   current_session_id: contextvars.ContextVar[str | None] = \
       contextvars.ContextVar("session_id", default=None)
   ```
2. In the trace injection helper (wherever `inject_trace_context` lives):
   - Before sending: read current `current_trace_id.get()` if caller didn't pass one.
3. In the trace extraction helper (`extract_trace_context`):
   - After successfully parsing, call `current_trace_id.set(trace_id)` and
     `current_session_id.set(session_id)`.
4. In `JsonFormatter.format` (already added in #6), the lines for `trace_id`
   and `session_id` should also check the ContextVar:
   ```python
   payload["trace_id"] = getattr(record, "trace_id", None) or current_trace_id.get()
   payload["session_id"] = getattr(record, "session_id", None) or current_session_id.get()
   ```
5. In MCP tool handlers, log the incoming trace if present:
   ```python
   logger.info("Tool called", extra={"tool": "get_prices", "ticker": ticker})
   ```
   The formatter pulls in `trace_id` from ContextVar automatically.

**Verification**
- Submit a query, capture the orchestrator's trace_id from Langfuse, then
  `grep <trace_id> logs/*.log` → returns lines from orchestrator, RAG, quant,
  sentiment, and MCP all stitched together.

**Risks**
- ContextVar doesn't survive `asyncio.create_task()` boundaries automatically
  in all Python versions. Mitigation: in tasks where it matters, explicitly
  pass the trace_id and re-set it inside the task.

**Rollback**: leave the ContextVar in place but stop reading it in the
formatter; logs revert to non-correlated.

---

## 14. Deduplicate ticker validation

**Goal**: Remove ~80 lines of copy-paste from 4 executors. Fix-once-runs-everywhere.

**Files to touch**
- `shared/ticker_utils.py` (add lifecycle-aware wrappers)
- `agent_1_adk/agent_executor.py:144-179` (replace `_validate_ticker`, `_resolve_ticker`, `_disconnect`)
- `agent_2_llamaindex/executor.py:131-149` (same)
- `agent_3_langgraph/executor.py:67-83` (same)
- `agent_4_crewai/executor.py:88-104` (same)

**Steps**

1. Depends on #11 (MCP singleton). After #11 lands, add to `shared/ticker_utils.py`:
   ```python
   from shared.mcp_client import get_shared_mcp

   async def validate_ticker(ticker: str) -> tuple[bool, str, str]:
       mcp = await get_shared_mcp()
       return await validate_ticker_via_mcp(mcp, ticker)

   async def resolve_ticker(query: str, exclude: str = "") -> tuple[str, str]:
       mcp = await get_shared_mcp()
       return await resolve_ticker_via_mcp(mcp, query, exclude)
   ```
2. In each of the 4 executors:
   - Replace calls to `self._validate_ticker(t)` with `await validate_ticker(t)`.
   - Replace calls to `self._resolve_ticker(q)` with `await resolve_ticker(q)`.
   - Delete the private methods.
3. Re-run the existing eval traces (or a fresh query) to confirm behavior.

**Verification**
- Query "Analyze NVDA" → ticker NVDA correctly validated.
- Query "Analyze the chip company NVIDIA" → resolver returns NVDA.
- Query with invalid ticker "Analyze ZZZZZ" → graceful error message.

**Rollback**: restore the per-executor private methods.

---

## 15. Unified `@logged` timing decorator

**Goal**: Consistent enter/exit/latency lines on hot paths without manual
`time.monotonic()` plumbing.

**Files to touch**
- `shared/logging_config.py` (add decorator)
- Apply selectively (don't carpet-bomb the codebase): MCP tools in
  `mcp_servers/finsight_server.py`, the `stream()` method in each sub-agent's
  executor.

**Steps**

1. In `shared/logging_config.py`:
   ```python
   import functools
   import time

   def logged(level: int = logging.INFO):
       def decorator(fn):
           @functools.wraps(fn)
           async def wrapper(*args, **kwargs):
               logger = logging.getLogger(fn.__module__)
               logger.log(level, "Enter %s", fn.__qualname__)
               t0 = time.monotonic()
               try:
                   result = await fn(*args, **kwargs)
                   elapsed = (time.monotonic() - t0) * 1000
                   logger.log(
                       level, "Exit %s (%.0fms)", fn.__qualname__, elapsed,
                       extra={"latency_ms": int(elapsed)},
                   )
                   return result
               except Exception as exc:
                   elapsed = (time.monotonic() - t0) * 1000
                   logger.log(
                       level, "Fail %s (%.0fms): %s",
                       fn.__qualname__, elapsed, exc,
                       extra={"latency_ms": int(elapsed)},
                   )
                   raise
           return wrapper
       return decorator
   ```
2. Annotate hot paths. **Do not** annotate every function — only:
   - All `@mcp.tool()` handlers in `finsight_server.py`
   - `executor.stream()` in each sub-agent
   - `SubAgentClient.send_message`

**Verification**
- Run a query, `grep "Exit" logs/*.log` → see latencies. JSON formatter
  emits `latency_ms` as a structured field.

**Risks**
- Over-decoration creates log noise. Keep scope tight.

**Rollback**: remove the `@logged()` annotations; decorator can stay unused.

---

## 16. Cancellation support

**Goal**: Aborted requests actually cancel. Currently `cancel()` raises
`NotImplementedError` and hung sub-agents stall the whole orchestrator for
180s.

**Files to touch**
- `shared/generic_executor.py:133-137`
- `agent_1_adk/agent_executor.py:360-361`
- `agent_1_adk/sub_agent_client.py` (per-agent timeout)
- `shared/config.py` (add per-agent timeout vars)

**Steps**

1. In `GenericAgentExecutor`:
   ```python
   def __init__(self, agent: BaseAgent):
       self.agent = agent
       self._task: asyncio.Task | None = None

   async def execute(self, context, event_queue):
       self._task = asyncio.current_task()
       try:
           ...
       except asyncio.CancelledError:
           await event_queue.enqueue_event(TaskStatusUpdateEvent(
               task_id=context.task_id,
               status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
           ))
           raise

   async def cancel(self, context, event_queue):
       if self._task and not self._task.done():
           self._task.cancel()
   ```
2. Mirror the same pattern in `FinSightAgentExecutor`.
3. In `shared/config.py`:
   ```python
   A2A_TIMEOUT_RAG = float(os.environ.get("A2A_TIMEOUT_RAG", "60.0"))
   A2A_TIMEOUT_QUANT = float(os.environ.get("A2A_TIMEOUT_QUANT", "90.0"))
   A2A_TIMEOUT_SENTIMENT = float(os.environ.get("A2A_TIMEOUT_SENTIMENT", "45.0"))
   ```
4. In `SubAgentClient.send_message`, pick timeout by agent name and wrap:
   ```python
   timeout_map = {
       "rag": A2A_TIMEOUT_RAG,
       "quant": A2A_TIMEOUT_QUANT,
       "sentiment": A2A_TIMEOUT_SENTIMENT,
   }
   timeout = timeout_map.get(agent_short_name, A2A_TIMEOUT)
   try:
       return await asyncio.wait_for(self._send(...), timeout=timeout)
   except asyncio.TimeoutError:
       return {"error": "agent_timeout", "agent": agent_name, "timeout": timeout}
   ```

**Verification**
- Add a deliberate `await asyncio.sleep(120)` in the RAG agent's `stream()`,
  set `A2A_TIMEOUT_RAG=10`, run a query → orchestrator gets back a clean
  `agent_timeout` error after ~10s, not 180s.

**Risks**
- Cancellation during a SQLite write could leave a partial state. Mitigation:
  use `write_lock()` from #3 — locks release on cancel, and writes are
  single-statement so atomicity is preserved.

**Rollback**: revert per-agent timeouts; restore `NotImplementedError` in
`cancel()`.

---

## 17. Test suite — pragmatic starter set

**Goal**: Starting from zero tests (per your note), build a *useful* test
foundation — not the 280-test giga-plan. Focus: pure functions, numerical
correctness, security boundary, and one integration smoke test.

**Prerequisite**: #12 (lazy OTel) must land first — otherwise pytest imports
trigger global instrumentation.

**Files to touch / create**
- `tests/conftest.py`
- `tests/unit/test_ticker_utils.py`
- `tests/unit/test_trace_context.py`
- `tests/unit/test_models.py`
- `tests/unit/test_ttl_cache.py` (after #5)
- `tests/unit/test_rate_limiter.py` (after #4)
- `tests/unit/memory/test_memory_store.py`
- `tests/unit/memory/test_ticker_memory.py`
- `tests/unit/test_quant_graph_nodes.py`
- `tests/integration/test_mcp_server_smoke.py`
- `tests/security/test_sandbox.py` (see #18 — split into its own activity)
- `pyproject.toml` (markers + asyncio mode)

**Steps**

1. `pyproject.toml` additions:
   ```toml
   [tool.pytest.ini_options]
   asyncio_mode = "auto"
   testpaths = ["tests"]
   markers = [
       "external: requires network (SEC, Yahoo)",
       "integration: requires running services",
       "security: security-critical tests",
   ]
   ```
2. `tests/conftest.py`:
   ```python
   import pytest
   import aiosqlite

   @pytest.fixture(autouse=True)
   def _clean_env(monkeypatch):
       monkeypatch.setenv("HF_HUB_OFFLINE", "1")
       monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
       monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
       monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")

   @pytest.fixture
   async def memory_db(tmp_path):
       from shared.memory.store import init_db
       conn = await aiosqlite.connect(":memory:")
       await init_db(conn)
       yield conn
       await conn.close()
   ```
3. **Phase A — pure functions** (no mocks needed):
   - `test_ticker_utils.py`: ~12 cases. Format validation (`AAPL`, `BRK.B`,
     too long, lowercase), stop-word rejection (`SEC`, `EPS`), extraction
     (parens, `$TICKER`, "for NVDA"), holdings extraction with/without
     `exclude`.
   - `test_trace_context.py`: inject → extract round-trip; reject malformed.
   - `test_models.py`: Pydantic model construction + `model_dump`/`model_validate`
     round-trips for `QueryContext`, `RAGInsights`, `QuantMetrics`,
     `InvestmentBrief`.
4. **Phase B — small mocked utilities**:
   - `test_ttl_cache.py`: hit, miss, expiry, single-flight (10 concurrent
     misses → 1 fetch).
   - `test_rate_limiter.py`: `acquire()` 10× immediately consumes the burst,
     then enforces rate.
5. **Phase C — memory layer** (in-memory SQLite):
   - `test_memory_store.py`: `init_db` creates tables, idempotent, schema_version.
   - `test_ticker_memory.py`: store_brief → get_latest → has_changed cycle.
6. **Phase D — numerical correctness** (quant graph nodes are pure):
   - `test_quant_graph_nodes.py`: feed known price arrays, assert Sharpe / VaR /
     CVaR / beta within tolerance. Mock LLM call in `llm_summary_node`.
7. **Phase E — integration smoke**:
   - `test_mcp_server_smoke.py` (marked `@pytest.mark.integration`):
     boot MCP server in-process, hit `/health`, call `validate_ticker("AAPL")`
     (needs network — also mark `@pytest.mark.external`).

**Target shape**: ~50–80 tests total, ~1000–1500 lines. Runs in <10s locally
for the non-network markers.

**Verification**
- `pytest tests/unit -v` → all green, <5s wall time.
- `pytest tests/ -m "not external"` → green without network.

**Risks**
- Phase D tests will be sensitive to random seeds / numerical precision.
  Mitigation: use `pytest.approx(..., rel=1e-3)`.

**Rollback**: delete `tests/` again; nothing else depends on it.

---

## 18. Security sandbox hardening + tests

**Goal**: `execute_python` is the most attackable surface in the system. It
already has AST-level blocking, but it needs explicit tests covering known
escape patterns. This is split out from #17 because it deserves its own
attention.

**Files to touch**
- Inspect `mcp_servers/finsight_server.py` `execute_python` implementation
  (search for the function definition).
- `tests/security/test_sandbox.py` (new)

**Steps**

1. Read the current `execute_python` implementation top-to-bottom. Document
   what's blocked (AST node types, import names, attribute names).
2. Write `tests/security/test_sandbox.py` with these cases (each should fail
   with a sandbox-block error, not actually execute):
   - `import os; os.system("dir")`
   - `__import__("os").system("dir")`
   - `open("test.txt", "w")`
   - `import subprocess`
   - `import socket; socket.socket()`
   - `eval("1+1")`
   - `exec("print('hi')")`
   - `().__class__.__bases__[0].__subclasses__()` (class-walking escape)
   - `compile("import os", "<test>", "exec")`
3. Positive cases (should succeed):
   - `2 + 2` → `4`
   - `[i**2 for i in range(10)]` → list
   - `import math; math.sqrt(2)` → 1.414...
4. Resource-bound cases:
   - Infinite loop → times out within a hard cap (e.g. 5s).
   - `[0] * 10**9` → memory cap triggers error, not OOM-kills the MCP server.
5. For any test that passes when it shouldn't, FILE A FIX before merging. Each
   class-walking or `__import__` bypass is a real CVE-class issue.

**Verification**
- `pytest tests/security -v` → all green.
- Manually try a known escape in the MCP tool from a client → blocked.

**Risks**
- Sandbox tightening can break legitimate quant computations. Mitigation:
  the positive-case tests above; add more if real workflows break.

**Rollback**: revert sandbox changes; tests will go red and you'll know the
posture got weaker.

---

## Skipped (with rationale)

The following items from the source docs are intentionally not detailed:

| # | Item | Rationale |
|---|------|-----------|
| — | `HF_HUB_OFFLINE` warning | Cosmetic. Current behavior matches actual deployment model (local cached models). |
| — | Async QueueHandler logging | "50–100ms disk stalls" claim is off by ~50× on local SSD. Premature optimization. |
| — | `AgentError` unification | Cosmetic. Doesn't unblock any feature. Three error shapes are ugly but functional. |
| — | Memory-persistence path consolidation | Mostly aesthetic once #3 (connection pooling) lands. SQLite triple-writes are not a real bottleneck. |
| — | Configurable LLM provider | YAGNI unless actively migrating off LM Studio. #2 handles 90% of the value. |
| — | Docker compose orchestration | `run_adk_web.bat` works; this is "nice to have" not "fixes a problem." |
| — | Full 280-test plan from `TEST_PLAN.md` | Sized for a production team. #17 is the right scope for this project. |

Revisit any of these if the project's scale or deployment target changes.

---

## Suggested execution sequence

A reasonable cadence (assuming solo dev, ~2–4 hrs/day):

| Day | Activities |
|-----|------------|
| 1 | #1, #2, #6, #7, #8 (config + logging foundation) |
| 2 | #3, #4, #5 (data layer + perf) |
| 3 | #9, #10 (persistence completeness) |
| 4 | #12 (lazy OTel — prereq for tests) |
| 5 | #11, #14 (MCP singleton + ticker dedup — paired since they touch the same files) |
| 6 | #13, #15 (observability polish) |
| 7 | #16 (cancellation) |
| 8 | #17 Phase A–C (pure + memory tests) |
| 9 | #17 Phase D–E + #18 (numerical + security tests) |

Total: ~8–9 working days end-to-end. Each item is independently shippable in
its own commit.
