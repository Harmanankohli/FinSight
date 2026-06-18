# Code Organization Remediation Plan

Auto-generated from code review. Each section is independently actionable.
An agent should tackle items in priority order, verifying each with `make test && make lint && make type` before moving to the next.

---

## P1 — Structural Fixes (High Priority)

### 1.1 Move `src/seed_user.py` into `src/scripts/`

 **Files to touch:**
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\seed_user.py`

**Action:**
1. Move `src/seed_user.py` → `src/scripts/seed_user.py`
2. **Fix `sys.path` hack**: line 13 has `sys.path.insert(0, "src")`. After moving into `src/scripts/`, change to `sys.path.insert(0, "..")` so the project root is still on the path.
3. Update the usage docstring on lines 4-5: `uv run python src/scripts/seed_user.py`
4. Grep for any remaining references to `src/seed_user.py` in docs/ (CHANGELOG.md, DESIGN_DECISIONS.md, AGENTS.md) and update them to `src/scripts/seed_user.py`.

**Verification:**
- `uv run python src/scripts/seed_user.py --help` prints usage (not `-m scripts.seed_user`, since that requires `__main__.py`)
- `scripts/` directory now contains 4 consistent files
- No dangling references to `src/seed_user.py`

---

### 1.2 [REMOVED — Stale] `contexts/lib/` nesting does not exist in current codebase

`contexts/lib/` directory not found. `AuthContext.tsx` already imports from `@/lib/auth`. No action needed.

---

### 1.3 Populate `shared/__init__.py` with re-exports

**Files to touch:**
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\shared\__init__.py`

**Action:**
Currently `shared/__init__.py` is empty. Every consumer must know the exact internal module path. Add explicit re-exports for the most commonly used symbols. Each import line needs `# noqa: F401` to suppress ruff's unused-import rule for re-exports:

```python
"""FinSight shared infrastructure — core utilities, base abstractions, and factories."""

from shared.base_agent import BaseAgent  # noqa: F401
from shared.settings import Settings, get_settings, reset_settings_for_tests  # noqa: F401
from shared.agent_server import build_agent_app  # noqa: F401
from shared.generic_executor import GenericAgentExecutor  # noqa: F401
from shared.bootstrap import bootstrap  # noqa: F401
from shared.logging_config import logged, logged_sync, logged_class, setup_file_logging  # noqa: F401
from shared.mcp_client import get_shared_mcp  # noqa: F401
from shared.observability import get_langfuse_client, init_instrumentation  # noqa: F401
from shared.guardrails import is_off_topic  # noqa: F401
from shared.ticker_utils import extract_ticker, resolve_and_validate_ticker, extract_holdings  # noqa: F401

__all__ = [
    "BaseAgent",
    "Settings",
    "get_settings",
    "reset_settings_for_tests",
    "build_agent_app",
    "GenericAgentExecutor",
    "bootstrap",
    "logged",
    "logged_sync",
    "logged_class",
    "setup_file_logging",
    "get_shared_mcp",
    "get_langfuse_client",
    "init_instrumentation",
    "is_off_topic",
    "extract_ticker",
    "resolve_and_validate_ticker",
    "extract_holdings",
]
```

Do NOT remove existing `from shared.xxx import YYY` statements from consumer files — this only adds a convenience path. Over time, the team can migrate to `from shared import YYY` style.

**Verification:**
- `import shared; shared.BaseAgent` works from any module
- `make lint` passes (ruff may complain about unused imports in `__init__`)
- `make test` passes

---

## P2 — Design & Maintainability (Medium Priority)

### 2.1 Extract Duplicate `_build_response()` Pattern into `BaseAgent`

**Files to touch:**
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\shared\base_agent.py`
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\quant\executor.py`
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\analytics\executor.py`
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\market_context\executor.py`
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\reviewer\executor.py`

**Problem:**

Every agent executor duplicates this pattern in `_build_response()`:

```python
async def _build_response(self, query: str) -> dict:
    trace_id, parent_span_id, query = extract_trace_ids(query)
    langfuse = get_langfuse_client()
    trace_ctx = (
        {"trace_id": trace_id, "parent_span_id": parent_span_id}
        if trace_id and parent_span_id
        else None
    )
    with langfuse.start_as_current_observation(
        as_type="span",
        name="<agent-name>-agent-stream",
        input=query,
        trace_context=trace_ctx,
    ) as span:
        if trace_id:
            trace_ctx = {"trace_id": trace_id, "parent_span_id": span.id}
        try:
            # ... agent-specific logic ...
        except Exception as e:
            logger.exception("<Agent> analysis failed")
            span.update(output={"error": str(e)})
            return {
                "response_type": "text",
                "is_task_complete": True,
                "is_error": True,
                "require_user_input": False,
                "content": f"<Agent> analysis failed: {e}",
            }
```

**Action:**

1. Add to `BaseAgent` (`src/shared/base_agent.py`):

```python
import traceback
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

@asynccontextmanager
async def _telemetry_span(self, span_name: str, query: str) -> AsyncIterator[tuple[dict | None, dict | None]]:
    """Open a Langfuse span, extract trace context from query, yield (trace_ctx, span) metadata.
    
    Usage inside subclass:
        async with self._telemetry_span("my-span", query) as (trace_ctx, span):
            ...
    """
    from shared.observability import get_langfuse_client
    from shared.trace_context import extract_trace_ids
    trace_id, parent_span_id, query = extract_trace_ids(query)
    langfuse = get_langfuse_client()
    trace_ctx = (
        {"trace_id": trace_id, "parent_span_id": parent_span_id}
        if trace_id and parent_span_id
        else None
    )
    with langfuse.start_as_current_observation(
        as_type="span",
        name=span_name,
        input=query,
        trace_context=trace_ctx,
    ) as span:
        if trace_id:
            trace_ctx = {"trace_id": trace_id, "parent_span_id": span.id}
        yield trace_ctx, span
```

2. Add a helper to build the standard error response dict:

```python
def _error_response(self, message: str) -> dict:
    return {
        "response_type": "text",
        "is_task_complete": True,
        "is_error": True,
        "require_user_input": False,
        "content": message,
    }

def _data_response(self, data: dict) -> dict:
    return {
        "response_type": "data",
        "is_task_complete": True,
        "is_error": False,
        "require_user_input": False,
        "content": data,
    }
```

3. Refactor each agent's `_build_response()` to use these helpers. Example for `quant/executor.py`:

```diff
-    @logged()
-    async def _build_response(self, query: str) -> dict:
-        trace_id, parent_span_id, query = extract_trace_ids(query)
-        langfuse = get_langfuse_client()
-        trace_ctx = (...)
-        with langfuse.start_as_current_observation(...) as span:
-            ticker, company = await resolve_and_validate_ticker(query)
+    async def _build_response(self, query: str) -> dict:
+        async with self._telemetry_span("quant-agent-stream", query) as (trace_ctx, span):
+            ticker, company = await resolve_and_validate_ticker(query)
             if not ticker:
                 span.update(output={"error": company or "No ticker found"})
-                return {...}
+                return self._error_response(company or "Could not identify a stock ticker.")
             ...
-            return {...}
+            return self._data_response(result)
```

4. **IMPORTANT: Keep `@logged()` decorator on `_build_response`**. The `@logged()` decorator provides entry/exit/latency logging that `_telemetry_span` does not replace. Keep both:
```python
    @logged()
    async def _build_response(self, query: str) -> dict:
        async with self._telemetry_span("quant-agent-stream", query) as (trace_ctx, span):
            ...
```
The `@logged()` decorator logs at function boundary level; `_telemetry_span` manages the Langfuse observation lifecycle. They serve different purposes.

5. Remove now-unused imports from each agent's `executor.py`:
   - `from shared.observability import get_langfuse_client`
   - `from shared.trace_context import extract_trace_ids`

**VERY IMPORTANT — per-agent differences to preserve:**
- **Quant** (`quant/executor.py:69-136`): Has holdings extraction via `extract_holdings()`, deterministic schema check via `score_quant_deterministic()`, and deferred eval via `_eval_quant_response`. Preserve all of these inside the telemetry span block.
- **Analytics** (`analytics/executor.py:43-109`): Uses `AnalyticsAgentOutput.model_validate(result)`, schema check via `score_analytics_deterministic()`, and deferred eval via `_eval_analytics`. Preserve.
- **Market Context** (`market_context/executor.py:149-220`): Has `contexts = result.pop("_retrieved_contexts", [])`, narrative extraction from multiple possible keys, and deferred eval via `_eval_sentiment_response`. Preserve.
- **Reviewer** (`reviewer/executor.py:39-218`): Uses `context_id` parameter (not just `query`), JSON deserialization of payload, inline agent outputs vs SQLite fetch, runs 4 deterministic tools, integrity gate, LLM synthesis via `Runner.run()`, and deferred eval. **Most complex** — ensure `_telemetry_span` works with extra args (add `context_id` as optional param). Also: reviewer returns `"content": json.dumps(output_dict)` (string, not dict). Call `self._data_response(json.dumps(output_dict))` to preserve this.

**Verification:**
- `make test` — all existing tests pass (especially `test_quant_nodes_io.py` and characterization tests)
- `make lint` — ruff clean
- `make type` — mypy clean on `src/shared` and `src/orchestrator`
- Spot-check: each agent's response dict shape preserved exactly (down to every key in the return dict)

---

### 2.2 Add `ruff format` to CI

**Files to touch:**
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\Makefile`
- (optionally) `.github/workflows/ci.yml`

**Action:**
Add to `Makefile`:

```makefile
fmt:     ruff format .
fmtcheck: ruff format --check .
```

**Do NOT add `fmtcheck` to the `ci` target yet.** Adding it before the codebase has been formatted means `make ci` fails immediately. The sequence is:

1. **This sprint**: Add `fmt` and `fmtcheck` targets to Makefile (no CI changes).
2. **After 3.3** (format entire codebase): Then add `fmtcheck` to the `ci` target.

For now, just add the targets. Update `ci` only after 3.3 is complete.

**Verification:**
- `make fmtcheck` reports formatting issues (expected, since codebase isn't formatted yet)
- `make fmt` runs without error
- `make lint` still passes after `make fmt`

---

### 2.3 Standardize Lazy vs Eager Import Style

**Files to touch:**
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\quant\executor.py`
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\analytics\executor.py`
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\market_context\executor.py`
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\financial_rag\executor.py`
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\reviewer\executor.py`
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\orchestrator\agent_executor.py`

**Problem:**

Inconsistent import pattern for `defer_eval` and `score_*` functions across agent executors:

| File | Pattern |
|------|---------|
| `quant/executor.py:10` | `from shared.runtime_eval import score_quant_response as _eval_quant_response` (eager, top-level) |
| `quant/executor.py:124` | `from shared.eval_gate import defer_eval` (lazy, inside `if EVAL_ENABLED:` block) |
| `analytics/executor.py:84` | `from shared.eval_gate import defer_eval` and `from shared.runtime_eval import ...` (both lazy) |
| `financial_rag/executor.py:12` | `from shared.runtime_eval import score_rag_response as _eval_rag_response` (eager, top-level) |
| `financial_rag/executor.py:253` | `from shared.eval_gate import defer_eval` (lazy, inside guard) |
| `market_context/executor.py:196` | `from shared.eval_gate import defer_eval` (lazy) |
| `reviewer/executor.py:201` | `from shared.eval_gate import defer_eval` and `from shared.runtime_eval import ...` (both lazy) |
| `orchestrator/agent_executor.py:24` | `from shared.runtime_eval import score_response as _eval_score_response` (eager) |

**Action:**

All deferred-eval related imports should be lazy (inside the `if EVAL_ENABLED:` guard) since they add import overhead for runtime paths that rarely execute. Remove the eager imports and move them into the guard block consistently.

For each file:
1. Remove `from shared.eval_gate import defer_eval` from module-level imports
2. Remove `from shared.runtime_eval import score_*` from module-level imports
3. Ensure these imports exist inside the `if EVAL_ENABLED:` block where they're used

Exception: `src/shared/runtime_eval.py` — do NOT touch this file; it's the definition site.

**Verification:**
- `make test` — especially eval-gated tests
- `make lint`
- `make type`

---

## P3 — Minor & Nice-to-Have (Low Priority)

### 3.1 Fix `market_context/executor.py:224` — Inline `_extract_retrieved_contexts`

**Files to touch:**
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\market_context\executor.py`

**Action:**
Move `_extract_retrieved_contexts` from a module-level function (decorated with `@logged_sync()`) into `MarketContextAgent` as a static method or private method. This makes it consistent with the other agents where helper functions are class methods.

```diff
-class MarketContextAgent(BaseAgent):
+class MarketContextAgent(BaseAgent):
+    @staticmethod
+    def _extract_retrieved_contexts(data: dict) -> list[str]:
+        ...
```

And update the call site (line 126):

```diff
-        result["_retrieved_contexts"] = _extract_retrieved_contexts(data)
+        result["_retrieved_contexts"] = self._extract_retrieved_contexts(data)
```

Remove the standalone `@logged_sync()` decorator from the function (it's a pure data transformation, no side effects worth logging at that granularity).

**Verification:**
- `make test` — market context tests pass
- `make lint`

---

### 3.2 Fix `shared/memory/__init__.py` — Replace `__getattr__` with Explicit Imports

**Files to touch:**
- `C:\Users\harma\OneDrive\Documents\Code\FinSight\multi-agent-investment-system\src\shared\memory\__init__.py`

**Action:**
Replace the `__getattr__` dynamic dispatch (which breaks static analysis) with direct imports:

```diff
-from shared.memory.store import DB_PATH, get_db, init_db
-from shared.memory.ticker_memory import TickerMemory
-
-
-def __getattr__(name: str):
-    if name == "SQLiteMemoryService":
-        from shared.memory.memory_service import SQLiteMemoryService
-        return SQLiteMemoryService
-    if name == "PerformanceTracker":
-        from shared.memory.performance_tracker import PerformanceTracker
-        return PerformanceTracker
-    if name == "PortfolioStore":
-        from shared.memory.portfolio_store import PortfolioStore
-        return PortfolioStore
-    if name in ("store_agent_output", "get_agent_outputs", "prune_stale_outputs"):
-        from shared.memory.agent_output_store import (
-            get_agent_outputs,
-            prune_stale_outputs,
-            store_agent_output,
-        )
-        return locals()[name]
-    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
+from shared.memory.store import DB_PATH, get_db, init_db
+from shared.memory.ticker_memory import TickerMemory
+from shared.memory.memory_service import SQLiteMemoryService
+from shared.memory.performance_tracker import PerformanceTracker
+from shared.memory.portfolio_store import PortfolioStore
+from shared.memory.agent_output_store import (
+    get_agent_outputs,
+    prune_stale_outputs,
+    store_agent_output,
+)
```

**Verification:**
- `import shared.memory; shared.memory.PortfolioStore` works
- `make test` passes
- `make type` — no more `"SQLiteMemoryService" is not exported from module` warnings
- Check that no circular import errors appear at runtime (run `uv run python -c "from shared.memory import SQLiteMemoryService"`)

---

### 3.3 One-Off: Format Entire Codebase with `ruff format`

**Action (only after 2.2 is done):**
1. `cd` to project root
2. `uv run ruff format .`
3. Review the diff — it should only be whitespace/line-break changes (ruff format is very safe)
4. `make lint && make test && make type`

This step is optional and should be a separate commit from any logic changes. It touches many files but is purely mechanical.

---

### 3.4 Harmonize agent_card definitions

**Problem:**
- Each `src/*/server.py` defines `agent_card = AgentCard(...)` in code
- `agent_cards/*.json` duplicates the same info as JSON files
- If they diverge, A2A discovery returns wrong metadata

**Short-term fix (this sprint):**
Add a comment at the top of each `server.py` that says:
```python
# Agent card: keep in sync with agent_cards/<name>.json
```

**Long-term (future):**
Generate `agent_cards/*.json` from the Python definitions at build time. Not implementing now — just document as known tech debt.

**Action:**
Add the sync-warning comment to each `server.py` file (quant, analytics, market_context, reviewer, financial_rag, orchestrator).

---

## P4 — Future Architecture (Not for This Sprint)

These are noted but explicitly out of scope for this plan:

### 4.1 Split `orchestrator/agent_executor.py` — God Class
- 624 lines, 7+ responsibilities
- Would require: extract `TickerCacheService`, `GuardrailChain`, `MemoryPersistenceService`, `OrchestrationService`
- Risk: high. Tests heavily depend on `FinSightAgentExecutor`. Skip for now.

### 4.2 `shared/` module splitting
- Growing to 30+ modules in one package
- Consider `finsight_lib/` for utility code (ticker_utils, rate_limiter, ttl_cache, sandbox) separate from `finsight_shared/` for infrastructure (settings, base_agent, agent_server, logging, auth)
- Skip for now — not urgent

---

## Verification Checklist

After ALL changes:

| Check | Command |
|-------|---------|
| Lint | `make lint` — ruff clean |
| Type check | `make type` — mypy clean on shared + orchestrator |
| Unit + characterization tests | `make test` — all pass |
| Frontend build | `cd src/web/nextjs-app && npm run build` |
| All agent startup | `uv run python -c "from quant.executor import QuantAgent; QuantAgent()"` (repeat for each agent) |
| Shared imports work | `uv run python -c "from shared import BaseAgent, get_settings, build_agent_app"` |

---

## Rollback Plan

1. Each change above is independent — revert individual commits if one breaks
2. The `ruff format` step (3.3) should be its own commit at the end, NOT squashed with logic changes
3. Tests are the safety net: if `make test` passes, the change is safe

---

## Code Review — Plan Correctness & Safety (2026-06-18)

Verified each item against current codebase state.

### Item 1.1 — `seed_user.py` move
- File exists with `sys.path.insert(0, "src")` on line 13. After moving into `src/scripts/`, this path breaks — must update to `sys.path.insert(0, "..")` or remove if pyproject.toml handles it.
- No references in Makefile, shell scripts, or CI — only in docs (CHANGELOG, DESIGN_DECISIONS, AGENTS.md). Safe.
- Verification command `uv run python -m scripts.seed_user --help` requires `src/scripts/__main__.py` which doesn't exist. Correct command: `uv run python src/scripts/seed_user.py --help`.

### Item 1.2 — `contexts/lib/` nesting
**STALE. Remove from plan.** The directory `src/web/nextjs-app/contexts/lib/` does not exist. `AuthContext.tsx` already imports from `@/lib/auth` (line 8). The `lib/` directory already has the correct structure. No files reference `contexts/lib/` anywhere.

### Item 1.3 — `shared/__init__.py` re-exports
- `shared/__init__.py` is confirmed empty.
- No circular import risk: memory submodules import from `shared.memory.store`, `shared.models`, `shared.settings` — none import from `shared` (top-level).
- **Caveat**: Ruff F401 (unused imports) will fire on `__init__.py` re-exports. Add `# noqa: F401` to each import line or add per-file-ignore in pyproject.toml.

### Item 2.1 — Extract `_build_response()` into `BaseAgent`
Verified all 4 executors. Plan's per-agent difference notes are accurate.
- **`@logged()` decorator**: All 4 executors decorate `_build_response` with `@logged()`. Plan's diff shows removing it. Confirm whether `@logged()` should be removed or kept alongside `_telemetry_span`. Removing it loses the entry/exit log lines.
- **Reviewer return shape**: Reviewer returns `"content": json.dumps(output_dict)` (string, not dict). The `_data_response` helper passes `data` as-is. This is fine as long as reviewer calls `self._data_response(json.dumps(output_dict))`, but plan doesn't show this explicitly.
- **`BaseAgent` is Pydantic BaseModel**: Adding `_telemetry_span` (asynccontextmanager), `_error_response`, `_data_response` as methods is fine. No BaseModel field conflicts.

### Item 2.2 — Add `ruff format` to CI
**WILL BREAK CI as written.** Adding `fmtcheck` to `ci` target before running `ruff format` on the codebase means `make ci` fails immediately. Either:
- (a) Run `ruff format .` first, then add `fmtcheck` to `ci`, OR
- (b) Add `fmt` and `fmtcheck` targets but do NOT add `fmtcheck` to `ci` until 3.3 is done.

### Item 2.3 — Standardize lazy vs eager imports
- Missing `financial_rag/executor.py` from the file list. It has `from shared.runtime_eval import score_rag_response as _eval_rag_response` (eager, line 12) and `from shared.eval_gate import defer_eval` (lazy, line 253). Same pattern as the others — needs the same treatment.
- The `score_*_deterministic` functions (e.g., `score_quant_deterministic`) are called outside `EVAL_ENABLED` guards, so they correctly remain eager. Plan is correct to only move `score_*_response` and `defer_eval`.
- `orchestrator/agent_executor.py:24` has `from shared.runtime_eval import score_response as _eval_score_response` (eager) — plan mentions this, correct.

### Item 3.1 — Inline `_extract_retrieved_contexts`
Safe. Module-level `@logged_sync()` function at line 223-264, call site at line 126. Moving to `@staticmethod` is straightforward.

### Item 3.2 — Replace `__getattr__` in `shared/memory/__init__.py`
Safe. No circular import risk confirmed. `__all__` already lists all exported names.

### Item 3.3 — Format codebase
Depends on 2.2. Purely mechanical whitespace. Safe.

### Item 3.4 — Harmonize agent_card
Comment-only change. Safe.

### Risk Summary

| Change | Risk | Reason |
|--------|------|--------|
| 1.1 seed_user move | Low | Only path hack needs updating |
| 1.2 contexts/lib | N/A | Stale — skip |
| 1.3 shared/__init__ re-exports | Low | Add noqa for F401 |
| 2.1 _build_response extraction | Medium | Core agent logic refactored; test thoroughly |
| 2.2 ruff format CI | High | Breaks CI if fmtcheck added before format run |
| 2.3 lazy imports | Low | Import path changes only, no logic change |
| 3.1 inline function | Low | Simple method move |
| 3.2 memory __getattr__ | Low | Direct replacement |
| 3.3 format codebase | Low | Mechanical |
| 3.4 agent_card comments | None | Comment-only |
