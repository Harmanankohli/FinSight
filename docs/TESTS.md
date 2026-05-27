# Test Coverage

**~148 test cases across 11 test files.**

## Running Tests

```bash
pytest                          # all unit tests
pytest -v                       # verbose
pytest tests/unit/              # unit tests only
pytest -m integration           # integration smoke tests (requires running services)
pytest -m "not integration"     # unit tests only (default)
```

## Test Layout

```
tests/
├── conftest.py                          # env isolation, memory_db fixture
├── integration/
│   └── test_mcp_server_smoke.py         # 4 integration smoke tests
├── security/
│   └── test_sandbox.py                  # ~60 — AST gate + subprocess sandbox
└── unit/
    ├── test_models.py                   # 10 — Pydantic model construction, round-trip
    ├── test_quant_graph_nodes.py        # 18 — compute_metrics, stress_test, format_output
    ├── test_ticker_utils.py             # 11 — ticker format, extraction, holdings parsing
    ├── test_ttl_cache.py                # 9 — cache hit/miss/expiry, single-flight, LRU eviction
    ├── test_rate_limiter.py             # 4 — burst, rate enforcement, refill
    ├── test_trace_context.py            # 8 — inject/extract round-trip, edge cases
    └── memory/
        ├── test_memory_store.py         # 5 — table creation, indexes, WAL mode, idempotency
        └── test_ticker_memory.py        # 7 — store/get_latest, history, flip detection

**Total: ~148 test cases across 11 test files.**

## Key Patterns

- **Env isolation**: `conftest.py` monkeypatches `LLM_API_KEY`, `LLM_BASE_URL`, `LANGFUSE_*` so no real services are contacted.
- **Isolated SQLite**: `memory_db` fixture resets the `shared.memory.store` module-level connection singleton per test, using `tmp_path` for unique DB files.
- **No network calls**: Quant graph tests call LangGraph nodes directly with synthetic `numpy` price data. `mcp_client=None` forces beta to `1.0`.
- **Asyncio**: All tests use `pytest-asyncio` with `asyncio_mode = "auto"` and `asyncio_default_fixture_loop_scope = "function"`.

## Integration Tests

4 smoke tests marked `@pytest.mark.integration` + `@pytest.mark.external` — skipped by default. Run with:

```bash
pytest -m integration
```

These verify the MCP server (port 8010) and agent card endpoints (ports 8002–8004) are reachable. Requires all services running (`run_adk_web.bat`).

## Test Configuration

Defined in `pyproject.toml`:
- `asyncio_mode = "auto"`
- `asyncio_default_fixture_loop_scope = "function"`
- Custom markers: `integration`, `external`
