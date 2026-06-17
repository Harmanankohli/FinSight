# Plan: Shared Agent Output Store + Reviewer Tool Fix

## Context

Two problems solved together:

1. **Slow Phase 2 handoff**: After Phase 1, the orchestrator LLM reads ~10K tokens of agent output just to copy-paste it into a JSON payload for the reviewer. With a local 14B model, this wastes 30-60s.

2. **Reviewer tool crash** (blocking bug): The OpenAI Agents SDK v0.17.5 rejects plain Python functions as tools with `ChatCompletions` API — `Hosted tools are not supported`. The reviewer's 4 tools (`check_contradictions`, `verify_sources`, `score_confidence`, `validate_recommendation`) are all plain functions, so the reviewer is completely broken.

**Solution**: Store full agent outputs in shared SQLite (both processes already use `db/finsight_memory.db`). The reviewer executor fetches them directly and calls the 4 tools in Python — no LLM tool-call loop, no SDK incompatibility, no token waste.

## New Flow

```
Phase 1:  send_message() stores full output in SQLite, returns trimmed summary to LLM
Phase 2:  LLM sends {"ticker": "AAPL", "session_id": "abc123"} to reviewer
          Reviewer executor fetches full data from SQLite
          Executor calls 4 tools directly in Python (no LLM round-trips)
          Executor calls LLM once for synthesis only (no tools on the agent)
Phase 3:  Orchestrator LLM synthesizes final recommendation
```

## Changes

### 1. New table in `src/shared/memory/store.py`

Bump `SCHEMA_VERSION` to 6. Add to `CREATE_TABLES_SQL`:

```sql
CREATE TABLE IF NOT EXISTS agent_output_store (
    session_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    output_json TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (session_id, agent_name)
);
CREATE INDEX IF NOT EXISTS idx_aos_session ON agent_output_store(session_id);
```

Add v5→v6 migration block following the existing pattern.

Add `agent_output_store` to `prune_old_records()` allowed tables.

### 2. New file `src/shared/memory/agent_output_store.py`

Functions:
- `store_agent_output(session_id, agent_name, output: dict)` — INSERT OR REPLACE
- `get_agent_outputs(session_id) -> dict[str, dict]` — returns `{"quant": {...}, "rag": {...}, ...}` with normalized keys
- `prune_stale_outputs(max_age_seconds=600)` — TTL cleanup

Key mapping (`_normalize_agent_key`): `"Financial RAG Agent"` → `"rag"`, `"Quant Analysis Agent"` → `"quant"`, etc. Same mapping already used in `agent_executor.py:521-534`.

### 3. Modify `send_message()` in `src/orchestrator/agent.py`

After storing in `_agent_responses` (in-memory), also persist to SQLite:

```python
await store_agent_output(session_id, resolved, parsed)
```

The trimmed return to the LLM (already implemented) stays unchanged.

### 4. Inject session_id into LLM prompt in `src/orchestrator/agent.py`

- `_build_instruction(session_id=None)` — append `Your current session_id is: {session_id}` when available
- `_instruction_provider(ctx)` — extract `ctx.session.id` and pass to `_build_instruction`

### 5. Update Phase 2 instruction in `_STATIC_PREAMBLE`

From: `Pass a JSON payload: {"ticker": "AAPL", "agent_outputs": {...}}`
To: `Pass a JSON payload: {"ticker": "AAPL", "session_id": "<your_session_id>"}`

### 6. Rewrite reviewer agent & executor

**`src/reviewer/agent.py`**: Remove tools from the agent. Agent becomes synthesis-only:

```python
reviewer_agent = Agent(
    name="Reviewer",
    model=_model,
    instructions="You are an investment analysis reviewer. Given pre-computed validation results, synthesize into a verdict.",
    output_type=ReviewerAgentOutput,
)
```

No tools, no input guardrails (guardrail logic moves to executor), no output guardrails (confidence check moves to executor).

**`src/reviewer/executor.py`**: `_build_response()` changes to:

1. Parse `session_id` and `ticker` from query JSON
2. Fetch `agent_outputs` from SQLite via `get_agent_outputs(session_id)`
3. Call 4 tools directly in Python: `check_contradictions(agent_outputs)`, `verify_sources(agent_outputs)`, `score_confidence(agent_outputs)`, `validate_recommendation(agent_outputs)`
4. Build a compact synthesis prompt with tool results (not the raw agent data)
5. Call `Runner.run(reviewer_agent, input=synthesis_prompt)` — single LLM call, no tools
6. Validate confidence range in Python (replaces output guardrail)

This eliminates 5 LLM round-trips → 1, fixes the SDK tool crash, and the reviewer gets the full untrimmmed data from SQLite.

### 7. Update reviewer guardrails `src/reviewer/guardrails.py`

Accept `session_id` as alternative to `agent_outputs` in payload validation. Or remove guardrails entirely since the executor now validates before calling the LLM.

### 8. Startup cleanup in `src/orchestrator/main.py`

Add `prune_stale_outputs()` call alongside existing `prune_old_records()` in lifespan.

### 9. Export from `src/shared/memory/__init__.py`

Add lazy import for `AgentOutputStore` functions.

## Files Modified

| File | Change |
|------|--------|
| `src/shared/memory/store.py` | New table, schema v6, migration, prune list |
| `src/shared/memory/agent_output_store.py` | **New** — store/retrieve/prune API |
| `src/shared/memory/__init__.py` | Add to exports |
| `src/orchestrator/agent.py` | Persist to store, inject session_id, update Phase 2 instructions |
| `src/orchestrator/main.py` | Add stale output pruning at startup |
| `src/reviewer/agent.py` | Remove tools, simplify to synthesis-only agent |
| `src/reviewer/executor.py` | Fetch from store, call tools in Python, single LLM call |
| `src/reviewer/guardrails.py` | Accept session_id or remove (logic moves to executor) |

## Verification

1. Run unit tests: `uv run python -m pytest src/tests/unit/ -x -q`
2. Start servers and run an end-to-end analysis query
3. Verify reviewer logs show "fetched N agent outputs from shared store"
4. Verify reviewer completes without `Hosted tools are not supported` error
5. Verify final synthesis still contains BUY/HOLD/SELL with confidence score
