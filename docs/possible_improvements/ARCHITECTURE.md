# Architecture Improvements

Twelve targeted improvements to the current architecture, excluding the
framework-proliferation topic (design intent). Ordered by category —
each includes problem location, proposed change, and effort estimate.

---

## Data & Connections

### 1. SQLite Connection Pooling

**Effort**: small (~20 lines, 1 file)

**Problem**: `shared/memory/store.py:84-96` — `get_db()` opens a brand-new
`aiosqlite` connection on every call with no pooling. Every ticker lookup,
portfolio read, filing-dedup check, and performance write creates + tears
down a connection. SQLite serializes all writes even with WAL mode — under
concurrent requests, `busy_timeout=5000` causes 5-second stalls.

```
# Current: new connection per call
conn = await get_db()       # open
cursor = await conn.execute(...)
result = cursor.fetchone()
await conn.close()          # close
```

**Proposed change**: Use a single long-lived connection with an
`asyncio.Lock` for writes.

```python
# shared/memory/store.py — proposed:
import asyncio

_db_conn = None
_db_lock = asyncio.Lock()

async def get_db(path=DB_PATH):
    global _db_conn
    if _db_conn is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _db_conn = await aiosqlite.connect(str(path))
        await _db_conn.execute("PRAGMA journal_mode=WAL")
        await _db_conn.execute("PRAGMA foreign_keys=ON")
        await _db_conn.execute("PRAGMA busy_timeout=5000")
        await init_db(_db_conn)
    return _db_conn

# Callers that write must acquire the lock:
async with _db_lock:
    conn = await get_db()
    await conn.execute("INSERT ...")
```

**Files to touch**: `shared/memory/store.py`, `ticker_memory.py`,
`portfolio_store.py`, `performance_tracker.py`, `memory_service.py` —
remove `await conn.close()` from all callers.

---

### 2. MCP Client as Long-Lived Singleton

**Effort**: medium (~50 lines across 4 files)

**Problem**: Every sub-agent creates its own `MCPClient`, connects, does
work, then disconnects — per request. The orchestrator does the same
for ticker validation. SSE connections involve an HTTP upgrade handshake
(~100-500ms). Connect/disconnect happens on every A2A request.

Locations:
- `agent_2_llamaindex/executor.py:109-118`
- `agent_3_langgraph/executor.py:32-37`
- `agent_4_crewai/executor.py:36-45`
- `agent_1_adk/agent_executor.py:146-179` (temporary MCP)

**Proposed change**: Module-level singleton in `shared/mcp_client.py`,
initialized once at import with lazy connect on first tool call.

```python
# shared/mcp_client.py — proposed singleton:
_global_client = None
_client_lock = asyncio.Lock()

async def get_mcp_client() -> MCPClient:
    global _global_client
    if _global_client is None:
        async with _client_lock:
            if _global_client is None:
                _global_client = MCPClient(...)
                await _global_client.connect_all()
    return _global_client
```

Remove `_ensure_connected` / `_disconnect` from all 4 agent executors.

---

### 3. Rate Limiting

**Effort**: small (~40 lines, 1 new file)

**Problem**: No rate limiting. 10 concurrent users → 10 simultaneous
yfinance calls + 10 SEC EDGAR requests. SEC rate-limits to ~10 req/s
with hard bans. Yahoo returns 429 under load.

Hot paths:
- `finsight_server.py:272-280` — `get_prices()`
- `finsight_server.py:475-490` — `_fetch_submissions()`
- `finsight_server.py:1152-1180` — `_fetch_rss()`

**Proposed change**: Token-bucket rate limiter in `shared/`, applied to
`_EdgarClient` and RSS fetcher.

```python
# shared/rate_limiter.py — proposed:
import asyncio, time

class TokenBucket:
    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
        await asyncio.sleep(1.0 / self.rate)
        return await self.acquire()

_sec_limiter = TokenBucket(rate=8, burst=10)
```

---

## Security & Configuration

### 4. SEC EDGAR User-Agent Compliance

**Effort**: trivial (~1 line)

**Problem**: `mcp_servers/finsight_server.py:358` — SEC headers use
`contact@finsight.com` which doesn't resolve. SEC blocks non-compliant
User-Agents.

```python
_SEC_HEADERS = {
    "User-Agent": "FinSight Research (contact@finsight.com)",
}
```

**Proposed change**: Read from env var with a dev-mode warning.

```python
import os
_user_agent = os.environ.get(
    "SEC_USER_AGENT",
    "FinSight Research (dev-mode — set SEC_USER_AGENT env var)"
)
if "dev-mode" in _user_agent:
    logger.warning("SEC User-Agent is placeholder — may be blocked")
_SEC_HEADERS = {"User-Agent": _user_agent}
```

---

### 5. Hardcoded `api_key="lmstudio"`

**Effort**: trivial (~4 files, 1 line each)

**Problem**: Hardcoded in 4 files. Maintenance hazard, present in DEBUG
logs, no env var.

Locations:
- `agent_1_adk/agent.py:21`
- `agent_2_llamaindex/index_manager.py:28`
- `agent_4_crewai/crew.py:15`
- `agent_3_langgraph/nodes.py`

**Proposed change**: Read from `shared/config.py` as `LLM_API_KEY` env var.

```python
# shared/config.py — add:
LLM_API_KEY = os.environ.get("LLM_API_KEY", "lmstudio")

# Every agent reads from config instead of hardcoding.
```

---

### 6. Hardcoded `HF_HUB_OFFLINE=1`

**Effort**: small (~5 lines, 1 file)

**Problem**: `shared/config.py:28` forces offline mode. If models aren't
cached locally, the system fails silently — no RAG, no semantic cache,
no agent registry search.

**Proposed change**: Keep default but add a startup check that warns
about missing models with instructions to pre-download or set
`HF_HUB_OFFLINE=0`.

```python
# shared/config.py — proposed addition:
_MODEL_CHECKED = False

def check_embedding_models():
    global _MODEL_CHECKED
    if _MODEL_CHECKED:
        return
    _MODEL_CHECKED = True
    from pathlib import Path
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
    model_ids = [
        f"sentence-transformers/{EMBED_MODEL}",
        f"cross-encoder/{RERANKER_MODEL.split('/')[-1]}",
    ]
    for mid in model_ids:
        safe = mid.replace("/", "--")
        model_path = cache_dir / f"models--{safe}"
        if not model_path.exists():
            logger.warning(
                "Model '%s' not cached at %s. Set HF_HUB_OFFLINE=0 or pre-download.",
                mid, model_path,
            )
```

---

## Code Quality

### 7. Duplicated Ticker Validation Logic

**Effort**: medium (~80 lines removed, ~20 added to shared/)

**Problem**: `_validate_ticker()`, `_resolve_ticker()`, `_disconnect()`
copy-pasted across all 4 agent executors. Bug fixes must be applied 4 times.

Locations:
- `agent_2_llamaindex/executor.py:131-149`
- `agent_3_langgraph/executor.py:67-83`
- `agent_4_crewai/executor.py:88-104`
- `agent_1_adk/agent_executor.py:144-179`

**Proposed change**: Move to `shared/ticker_utils.py` with convenience
wrappers that handle MCP lifecycle internally.

```python
# shared/ticker_utils.py — proposed additions:
async def validate_ticker(ticker: str) -> tuple[bool, str, str]:
    mcp = await _get_shared_mcp()
    return await validate_ticker_via_mcp(mcp, ticker)

async def resolve_ticker(query: str, exclude: str = "") -> tuple[str, str]:
    mcp = await _get_shared_mcp()
    return await resolve_ticker_via_mcp(mcp, query, exclude)
```

---

### 8. Inconsistent Error Propagation

**Effort**: medium (across 5+ files)

**Problem**: Same failure travels through 3 different error formats:

| Layer | Format |
|-------|--------|
| MCP tools | `{"error": str}` in result dict |
| GenericAgentExecutor | `TaskState.FAILED` + text message |
| SubAgentClient | `{"error": str}` JSON string |
| Orchestrator executor | Mixed — text or `error` key |

**Proposed change**: Standardize on `AgentError` class in
`shared/models.py` that all layers produce.

```python
# shared/models.py — proposed addition:
@dataclass
class AgentError:
    code: str
    message: str
    detail: dict | None = None

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message,
                "detail": self.detail or {}}
```

Scope is medium — touches every error-return path. Best done as a
dedicated refactor pass.

---

### 9. Import-Time Side Effects (OpenTelemetry)

**Effort**: small (~5 lines per file)

**Problem**: OpenTelemetry instrumentors fire at module import time:

- `agent_1_adk/sub_agent_client.py:12` — `HTTPXClientInstrumentor`
- `agent_2_llamaindex/server.py:26-29` — `LlamaIndexInstrumentor`
- `agent_3_langgraph/server.py:24-32` — `StarletteInstrumentor` + `LangChainInstrumentor`
- `agent_4_crewai/server.py:24-26` — `CrewAIInstrumentor` + `StarletteInstrumentor`

Importing any symbol triggers global instrumentation. Breaks test isolation.

**Proposed change**: Move to lazy `init_instrumentation()` called from
server entry point, not at module level.

```python
# shared/observability.py — proposed:
def init_instrumentation(agent_type: str) -> None:
    if agent_type == "orchestrator":
        HTTPXClientInstrumentor().instrument()
    elif agent_type == "rag":
        LlamaIndexInstrumentor().instrument()
        StarletteInstrumentor().instrument()
    ...

# Called from server main(), not at import.
```

---

### 10. Redundant Memory Persistence Paths

**Effort**: medium (~40 lines removed)

**Problem**: Conversation events written to memory 3 ways on every turn:

| Path | Location |
|------|----------|
| `after_agent_callback` | `agents/finsight_agent/agent.py:202-216` |
| Executor fallback | `agent_1_adk/agent_executor.py:253-257` |
| Direct persist | `agent_1_adk/agent_executor.py:507-530` |

Triples write load on SQLite. Makes memory behavior hard to reason about.

**Proposed change**: Consolidate to single path. Callback is canonical
for ADK Web UI. Executor paths gated by flag for standalone A2A mode.

---

## Reliability

### 11. Cancellation

**Effort**: medium (~60 lines across 2 files)

**Problem**: Both executors raise `NotImplementedError`:

- `shared/generic_executor.py:133-137`
- `agent_1_adk/agent_executor.py:360-361`

Requests cannot be aborted. A hung sub-agent blocks for 180s timeout.
No per-agent timeout.

**Proposed change**:

**11a. `GenericAgentExecutor.cancel()`** — Store the running `asyncio.Task`,
cancel it, emit `TASK_STATE_CANCELED`.

```python
class GenericAgentExecutor(AgentExecutor):
    def __init__(self, agent: BaseAgent):
        self.agent = agent
        self._task: asyncio.Task | None = None

    async def execute(self, context, event_queue):
        self._task = asyncio.current_task()
        ...

    async def cancel(self, context, event_queue):
        if self._task and not self._task.done():
            self._task.cancel()
            await event_queue.enqueue_event(TaskStatusUpdateEvent(
                task_id=...,
                status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
            ))
```

**11b. `FinSightAgentExecutor.cancel()`** — Same pattern: store task,
cancel, emit event.

**11c. Per-agent timeout in `SubAgentClient`** — Replace global
`A2A_TIMEOUT` with per-agent timeout via `asyncio.wait_for()`.

---

### 12. InMemoryTaskStore

**Effort**: small (documentation — known limitation)

**Problem**: Every agent uses `InMemoryTaskStore`:

- `agent_1_adk/main.py:75`
- `agent_2_llamaindex/server.py:85`
- `agent_3_langgraph/server.py:78`
- `agent_4_crewai/server.py:74`

A2A tasks in-flight during restart are lost. The orchestrator gets no
error — just timeout or "Agent failed".

**Proposed change**: Add `SQLiteTaskStore` (same pattern as
`shared/memory/store.py`) and use it in each sub-agent. Document as
low-priority — acceptable for development, needs fixing for production.

```python
# shared/store.py — proposed addition:
from a2a.server.tasks import TaskStore

class SQLiteTaskStore(TaskStore):
    """A2A TaskStore backed by SQLite for persistence across restarts."""
    ...
```

---

## Effort Summary

| # | Improvement | Effort | Files To Touch |
|---|-------------|--------|----------------|
| 1 | SQLite connection pooling | small | `shared/memory/store.py` + 4 modules |
| 2 | MCP client as singleton | medium | `shared/mcp_client.py` + 4 executors |
| 3 | Rate limiting | small | `shared/rate_limiter.py` (new) + `finsight_server.py` |
| 4 | SEC EDGAR User-Agent | trivial | `mcp_servers/finsight_server.py` |
| 5 | Hardcoded api_key | trivial | `shared/config.py` + 4 agent files |
| 6 | HF_HUB_OFFLINE check | small | `shared/config.py` |
| 7 | Deduplicate ticker logic | medium | `shared/ticker_utils.py` + 4 executors |
| 8 | Error propagation | medium | `shared/models.py` + 5+ files |
| 9 | Import-time side effects | small | `shared/observability.py` + 4 entry points |
| 10 | Redundant memory paths | medium | `agent_1_adk/agent_executor.py` + `agent.py` |
| 11 | Cancellation | medium | `generic_executor.py` + `agent_executor.py` + `sub_agent_client.py` |
| 12 | InMemoryTaskStore | small | `shared/store.py` (new) + 4 entry points |
