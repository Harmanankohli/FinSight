# Test Coverage

**~125 test functions (~180 parametrized cases) across 18 test files + offline evaluation driver. v1.39 adds 3 regression test files covering DOCX, HTML, and PPTX output formats.**

## Running Tests

```bash
pytest                          # all unit tests
pytest -v                       # verbose
pytest tests/unit/              # unit tests only
pytest -m integration           # integration/smoke tests (requires running services)
pytest -m "not integration"     # unit tests only (default)
```

## Test Layout

```
tests/
├── conftest.py                          # env isolation, memory_db fixture
├── evaluation/                          # offline RAGAS eval (standalone, not pytest)
│   ├── golden_set.jsonl                 #   5 golden examples (NVDA, AAPL)
│   └── run_offline_eval.py              #   offline evaluation driver
├── integration/
│   ├── test_mcp_server_smoke.py         #   4 — MCP + agent card endpoint reachability
│   └── test_behavioral_signals_e2e.py   #   5 — options flow, insider, positioning,
│                                        #       peer comparison, Monte Carlo (Phase 4)
├── security/
│   └── test_sandbox.py                  #  11 (parametrized ~45+) — AST gate + subprocess
├── regression/                          # Report generator regression tests (v1.39)
│   ├── test_docx_regression.py          #   5 — DOCX: valid output, empty, unknown,
│                                        #       unicode, markdown tables
│   ├── test_html_regression.py          #   8 — HTML: valid, empty, unknown, non-std rec,
│                                        #       unicode, tables, deck-stage embedded, XSS
│   └── test_pptx_regression.py          #   7 — PPTX: valid, empty, unknown, non-std rec,
│                                        #       unicode, tables, long summary
└── unit/
    ├── test_models.py                   #  10 — Pydantic model construction, round-trip
    ├── test_quant_graph_nodes.py        #  18 — compute_metrics, stress_test, Monte Carlo,
    │                                    #       format_output, peer_comparison, behavioral
    ├── test_parallel_dispatch.py        #   3 — concurrent gather, timeout map, key isolation
    ├── test_runtime_eval_gates.py       #   9 — circuit breaker, SHA-256 dedup, burst,
    │                                    #       kill switch, gate, deterministic (Phase 4)
    ├── test_ticker_utils.py             #  11 (parametrized ~15+) — ticker format, extraction,
    │                                    #       holdings parsing, financial stop words
    ├── test_ttl_cache.py                #   9 — cache hit/miss/expiry, single-flight, LRU
    ├── test_rate_limiter.py             #   4 — burst, rate enforcement, refill
    ├── test_trace_context.py            #   8 — inject/extract round-trip, edge cases
    └── memory/
        ├── test_memory_store.py         #   5 — table creation, indexes, WAL, idempotency
        ├── test_ticker_memory.py        #   7 — store/get_latest, history, flip detection
        └── test_save_brief_persists_synthesis.py  # 2 — synthesis-wins, rationale-fallback

**Total: ~180 parametrized test cases across 18 test files.**

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

## Report Generation Regression Tests (v1.39)

3 new test files validate all three output formats against realistic data patterns. All use `unittest.mock.patch("yfinance.Ticker", ...)` to avoid real network calls.

### Running

```bash
pytest tests/regression/ -v
```

### DOCX (`test_docx_regression.py`)

Tests `generate_docx()` with the shared `_extract_deck_data()` extraction pipeline. Validates output is valid `BytesIO`, non-empty, and handles edge cases (empty brief, unknown ticker, unicode, markdown tables).

### HTML (`test_html_regression.py`)

Tests `generate_html()` — verifies HTML structure (`<section>`, `</deck-stage>`), company name in title, deck-stage.js embedded inline (no external `src=`), CSS custom properties present, unicode encoding, and XSS prevention (`<script>` tags escaped as `&lt;script&gt;`).

### PPTX (`test_pptx_regression.py`)

Tests `generate_pptx()` — slide count verification via `_count_slides()` (parses PPTX zip structure for `ppt/slides/slide*.xml`). Validates ≥6 slides for realistic data, ≥3 slides for empty brief, handles long summaries without overflow.

## Test Configuration

Defined in `pyproject.toml`:
- `asyncio_mode = "auto"`
- `asyncio_default_fixture_loop_scope = "function"`
- Custom markers: `integration`, `external`
