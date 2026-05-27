# Architecture Improvements

Twelve targeted improvements to the current architecture, excluding the
framework-proliferation topic (design intent). Ordered by category —
each includes problem location, proposed change, and effort estimate.

---

## Data & Connections

### ~~1. SQLite Connection Pooling~~ ✅ IMPLEMENTED (v1.25)

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

### ~~2. MCP Client as Long-Lived Singleton~~ ✅ IMPLEMENTED (v1.25)

MCP client is now a process-wide singleton in `shared/mcp_client.py` via `get_shared_mcp()` with double-checked async lock, auto-reconnect on connection errors, and an `atexit` shutdown hook. All 4 executors use it.

### ~~3. Rate Limiting~~ ✅ IMPLEMENTED (v1.25)

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

### ~~4. SEC EDGAR User-Agent Compliance~~ ✅ IMPLEMENTED (v1.25)

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

### ~~5. Hardcoded `api_key="lmstudio"`~~ ✅ IMPLEMENTED (v1.25)

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

### ~~7. Duplicated Ticker Validation Logic~~ ✅ IMPLEMENTED (v1.25)

Ticker validation and resolution are now shared functions in `shared/ticker_utils.py`, using the MCP singleton. All 4 executors call the same code.

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

### ~~9. Import-Time Side Effects (OpenTelemetry)~~ ✅ IMPLEMENTED (v1.25)

OTel instrumentation is now lazy via `init_instrumentation()` in `shared/observability.py`. Imports are deferred; instrumentors fire at startup, not at module import.

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

### ~~11. Cancellation~~ ✅ IMPLEMENTED (v1.25)

Both executors now implement `cancel()` with `asyncio.Task.cancel()` + `TASK_STATE_CANCELED` event. Per-agent timeouts in `SubAgentClient` prevent one slow agent from stalling the entire pipeline.

### ~~12. InMemoryTaskStore~~ ✅ IMPLEMENTED (v1.25)

Tasks now persist across restarts via `SQLiteTaskStore` in `shared/a2a_store.py`. All four agent servers and the orchestrator use it.

## Effort Summary

| # | Improvement | Effort | Files To Touch |
|---|-------------|--------|----------------|
| 1 | SQLite connection pooling | small | `shared/memory/store.py` + 4 modules |
| 2 | MCP client as singleton → ✅ singleton+reconnect | medium | `shared/mcp_client.py` + 4 executors |
| 3 | Rate limiting | small | `shared/rate_limiter.py` (new) + `finsight_server.py` |
| 4 | SEC EDGAR User-Agent | trivial | `mcp_servers/finsight_server.py` |
| 5 | Hardcoded api_key | trivial | `shared/config.py` + 4 agent files |
| 6 | HF_HUB_OFFLINE check | small | `shared/config.py` |
| 7 | Deduplicate ticker logic → ✅ shared functions | medium | `shared/ticker_utils.py` + 4 executors |
| 8 | Error propagation | medium | `shared/models.py` + 5+ files |
| 9 | Import-time side effects → ✅ lazy OTel | small | `shared/observability.py` + 4 entry points |
| 10 | Redundant memory paths | medium | `agent_1_adk/agent_executor.py` + `agent.py` |
| 11 | Cancellation → ✅ cancel+timeouts | medium | `generic_executor.py` + `agent_executor.py` + `sub_agent_client.py` |
| 12 | InMemoryTaskStore → ✅ SQLiteTaskStore | small | `shared/a2a_store.py` + 4 entry points |
