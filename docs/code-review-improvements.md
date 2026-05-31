# Code Review: Planned Improvements

Branch: `code-review-fixes`
Generated from review of all Python source files on 2026-05-31.

---

## 🔴 Blocking (5)

### 1. Duplicate memory persistence — 3 paths store same data
**File:** `agent_1_adk/agent_executor.py:239-243`

`_add_events_to_memory()` and `_persist_to_memory()` both insert into `memory_entries` for the same events. The first calls `add_session_to_memory()`, the second calls `add_events_to_memory()` — both write-through to SQLite. Results in duplicate rows.

**Fix:** Remove `_persist_to_memory()` call or guard it with a dedup check.

### 2. `new_text_message()` called with 3 positional args
**File:** `agent_1_adk/agent_executor.py:185,197,327`

```python
new_text_message(cached_response, task.context_id, task.id)
```

The SDK helper signature is `new_text_message(text: str, role: int = 1)` — 2 positional args max. Passing 3 should raise `TypeError`.

**Fix:** Drop the third arg. Verify the SDK version to confirm signature.

### 3. Per-request DB connections bypass singleton
**File:** `agent_1_adk/api_routes.py:61,99`

`sessions_list` and `session_events` call `aiosqlite.connect()` on every request instead of using the shared `get_db()` singleton. No connection pooling, no cleanup on exception paths.

**Fix:** Refactor to use `get_db()` from `shared/memory/store.py`.

### 4. `TaskState.input_required` — wrong enum value
**File:** `shared/generic_executor.py:128`

Uses `TaskState.input_required` while all other code uses `TaskState.TASK_STATE_*` prefixed variants. The protobuf enum likely expects `TASK_STATE_INPUT_REQUIRED`.

**Fix:** Change to `TaskState.TASK_STATE_INPUT_REQUIRED`.

### 5. Overlapping condition hides `> 0.35` branch in risk scoring
**File:** `agent_3_langgraph/nodes.py:132-139`

```python
elif vol > 0.45:     score -= 0.3
elif vol > 0.35:     score -= 0.1   # ← dead code
```

The `> 0.35` branch is unreachable because `> 0.45` is checked first.

**Fix:** Swap order (check `> 0.35` before `> 0.45`) or merge thresholds.

---

## 🟡 Important (7)

### 6. `SemanticCache._ensure_ready()` — race condition
**File:** `shared/semantic_cache.py:52-67`

No lock guards lazy initialization of `self._col` and `self._embedder`. Two concurrent `get()` calls can both initialize.

**Fix:** Add `asyncio.Lock` with double-checked locking pattern.

### 7. `_get_fcf_from_financials` — picks any positive FCF, not most recent
**File:** `agent_3_langgraph/nodes.py:85-101`

Iterates via `financials_dict.items()` which may not be sorted. Returns first positive FCF found, potentially using a stale year.

**Fix:** Sort periods descending, return the most recent positive FCF.

### 8. `_weighted_vote` — zero scores drop weight silently
**File:** `agent_3_langgraph/nodes.py:376-393`

```python
present = {k: v for k, v in group_scores.items() if v != 0.0}
```

When DCF is unavailable (score = 0.0), its 20% weight disappears entirely. A single positive signal in a low-weight group can cross the BUY threshold.

**Fix:** Redistribute zero-signal weights proportionally across present signals, or require a minimum number of present signals.

### 9. Ticker memory API leaks data across users
**File:** `agent_1_adk/api_routes.py:30`

`tm.get_history(symbol, limit=limit)` called without `user_id` filter. Returns any user's briefs for that ticker.

**Fix:** Pass `user_id` from `_user_id(request)` to the query.

### 10. `"analyze"` appears twice in stop words frozenset
**File:** `shared/ticker_utils.py:27`

`"analyze"` is in the frozenset literal twice (one misspelled as part of another entry). Harmless but indicates copy-paste error.

**Fix:** Deduplicate the literal.

### 11. `format_context` labels all changes "upgraded/downgraded"
**File:** `shared/memory/ticker_memory.py:241`

Every recommendation change is labeled with both words regardless of direction.

**Fix:** Determine actual direction (upgrade vs downgrade) based on ordinal rank [SELL < HOLD < BUY].

### 12. Dead code: `_serialize_content()` function
**File:** `agent_1_adk/api_routes.py:139-161

Function defined but never called anywhere.

**Fix:** Remove or wire into actual event serialization.

---

## 🟢 Nits (6)

### 13. Inconsistent `golden_cross` type
**Files:** `agent_3_langgraph/nodes.py:560` (None), `nodes.py:622` (False)

`golden_cross` can be `True`, `False`, or `None` depending on which node populated it. The scoring function checks `is True / is False` but `None` passes both silently.

**Fix:** Normalize to always return `False` when data is unavailable.

### 14. `try/except` at class body level for `_TRANSIENT_EXC`
**File:** `shared/mcp_client.py:216-235`

If `httpx` import fails, `_TRANSIENT_EXC` is never defined.

**Fix:** Move to a module-level constant defined before the class.

### 15. `a2a_store.py` accesses private SDK attribute
**File:** `shared/a2a_store.py:57`

```python
self._mem._impl.tasks.setdefault(owner, {})[task.id] = task
```

Bypasses public API. Will break on SDK updates.

**Fix:** Use the public `save()` method instead of direct dict manipulation.

### 16. Untyped constructor attributes in `TokenBucket`
**File:** `shared/rate_limiter.py:10-14`

`rate`, `burst`, `tokens`, `last` have no type annotations.

**Fix:** Add type hints.

### 17. Hardcoded Yahoo Finance RSS URL
**File:** `mcp_servers/finsight_server.py:1224`

URL historically changes/deprecated by Yahoo.

**Fix:** Move to config/env var with current URL as default.

### 18. SQL injection via f-string in `prune_old_records`
**File:** `shared/memory/store.py:204`

```python
f"DELETE FROM {table} WHERE created_at < ?"
```

Currently safe (table comes from hardcoded list), but `# noqa: S608` flag indicates a linter warning.

**Fix:** Use a whitelist dict mapping table names to table names.

---

## 💡 Suggestions (3)

### 19. Extract shared scoring helper for fundamental metrics
**File:** `agent_3_langgraph/nodes.py:188-266`

`_score_fundamental_value` and `_score_fundamental_quality` are structurally identical — ~80 lines of duplicated logic.

**Suggestion:** Extract into `_score_fundamental_metrics(metrics_with_config: list[tuple])`.

### 20. Deduplicate RSI threshold buckets
**File:** `agent_3_langgraph/nodes.py:596-601` vs `nodes.py:288-300`

RSI computation and scoring use different threshold ranges. Should use a single config dict.

**Suggestion:** Define RSI bucket thresholds once and share between computation and scoring.

### 21. Extract guardrails into dedicated module
**File:** `agent_1_adk/agent_executor.py:128-298`

Off-topic filter, ticker validation, output length check, BUY/HOLD/SELL signal check — all inline in the executor.

**Suggestion:** Extract into `shared/guardrails.py` for testability.
