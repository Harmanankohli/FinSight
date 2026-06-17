# Test Coverage

**~275 test functions (~355 parametrized cases) across 34 test files + offline evaluation driver. v1.39 added DOCX/HTML/PPTX regression tests. Phase 0 added 45 characterization tests (4 files). Phase 3 added auth contract tests (3 files). Phase R added corpus regression harness (1 file). v2.2 added agent output extraction tests (1 file). v2.6 added 5 new test files for Analytics Agent, Reviewer Agent, and new agent models. v2.7 added agent_output_store memory test and Windows/locking fixes. All tests now live under `src/tests/`.**

## Running Tests

```bash
pytest                          # all unit tests
pytest -v                       # verbose
pytest src/tests/unit/              # unit tests only
pytest src/tests/characterization/  # characterization tests only
pytest -m auth                  # auth-related tests (Phase 3)
pytest -m openapi               # OpenAPI spec tests
pytest -m integration           # integration/smoke tests (requires running services)
pytest -m "not integration"     # unit tests only (default)
pytest -m "not external"        # exclude tests that need external services
```

## Test Layout

```
src/tests/
├── conftest.py                          # env isolation, memory_db fixture, os._exit hook
├── evaluation/                          # offline RAGAS eval (standalone, not pytest)
│   ├── golden_set.jsonl                 #   5 golden examples (NVDA, AAPL)
│   └── run_offline_eval.py              #   offline evaluation driver
├── characterization/                    # Phase 0 - 45 tests, 4 files
│   ├── conftest.py                      #   fixtures, mocks, golden loader
│   ├── test_api_contracts.py            #   ASGI in-process API route tests
│   ├── test_deck_extraction_golden.py   #   golden tests (4 fixtures, UPDATE_GOLDENS=1)
│   ├── test_mcp_tool_shapes.py          #   MCP tool return-shape contracts
│   └── test_quant_nodes_io.py           #   per-node state-in/state-out
├── integration/
│   ├── test_mcp_server_smoke.py         #   4 - MCP + agent card endpoint reachability
│   └── test_behavioral_signals_e2e.py   #   5 - options flow, insider, positioning,
│                                        #       peer comparison, Monte Carlo (Phase 4)
├── security/
│   └── test_sandbox.py                  #  11 (parametrized ~45+) - AST gate + subprocess
├── regression/                          # Report generator + corpus tests
│   ├── test_docx_regression.py          #   5 - DOCX: valid output, empty, unknown,
│                                        #       unicode, markdown tables
│   ├── test_html_regression.py          #   8 - HTML: valid, empty, unknown, non-std rec,
│                                        #       unicode, tables, deck-stage embedded, XSS
│   ├── test_pptx_regression.py          #   7 - PPTX: valid, empty, unknown, non-std rec,
│                                        #       unicode, tables, long summary
│   └── test_corpus_invariants.py        #   7 - Phase R: corpus fixture invariants
│                                        #       (hostile, one_liner, quant_heavy, etc.)
└── unit/
    ├── test_models.py                   #  10 - Pydantic model construction, round-trip
    ├── test_quant_graph_nodes.py        #  18 - quant metrics, signals, voting
    ├── test_parallel_dispatch.py        #   3 - concurrent gather, timeout map
    ├── test_runtime_eval_gates.py       #   9 - circuit breaker, dedup, burst
    ├── test_ticker_utils.py             #  11 (~15 parametrized) - extraction, parsing
    ├── test_ttl_cache.py                #   9 - hit/miss/expiry, LRU
    ├── test_rate_limiter.py             #   4 - burst, refill
    ├── test_trace_context.py            #   8 - inject/extract
    ├── test_settings.py                 #   Phase 1: pydantic-settings validation
    ├── test_deck_data_extraction.py     #  38 - Phase R: extraction pipeline edge cases
    ├── test_agent_outputs_extraction.py #  16 - v2.2: agent output capture + extraction routing
    ├── test_auth_tokens.py              # 132 - Phase 2: JWT gen, validation, rotation
    ├── test_auth_middleware.py          # 189 - Phase 2: middleware chain, routing
    ├── test_auth_routes.py              # 145 - Phase 2: login/refresh/logout, lockout
    ├── test_auth_audit.py               #  41 - Phase 2: structured auth.denied logs
    ├── test_user_store.py               # 146 - Phase 2: Argon2, user CRUD
    ├── test_auth_contract.py            # 170 - Phase 3: parametrized auth×route matrix
    ├── test_a2a_protocol.py             # 167 - Phase 3: A2A lifecycle, failure modes
    ├── test_openapi_spec.py             #  73 - Phase 3: spec regeneration check
    └── memory/
        ├── test_memory_store.py         #   5 - tables, indexes, WAL
        ├── test_ticker_memory.py        #   7 - store/get_latest, flip detection
        └── test_save_brief_persists_synthesis.py  # 2 - synthesis wins, rationale fallback
    └── test_agent_output_store.py   # 10 - v2.7: store/get/prune, cross-agent isolation, TTL expiry

**Total: ~355 parametrized test cases across 34 test files.**
```

## Key Patterns

- **Env isolation**: `conftest.py` monkeypatches `LLM_API_KEY`, `LLM_BASE_URL`, `LANGFUSE_*` so no real services are contacted.
- **Isolated SQLite**: `memory_db` fixture resets the `shared.memory.store` module-level connection singleton per test, using `tmp_path` for unique DB files.
- **No network calls**: Quant graph tests call LangGraph nodes directly with synthetic `numpy` price data. `mcp_client=None` forces beta to `1.0`.
- **Asyncio**: All tests use `pytest-asyncio` with `asyncio_mode = "auto"` and `asyncio_default_fixture_loop_scope = "function"`.
- **Windows file locking fix (v2.7)**: `test_auth_routes.py` uses `tmp_path` per-test database files to avoid SQLite locking on Windows where `NamedTemporaryFile` cannot be reopened while open.
- **DDG mock pattern (v2.7)**: `test_web_search_tool.py` mocks DuckDuckGo at the `aiohttp.ClientSession` level (not `DDGS`) to avoid real HTTP calls during CI.

## Integration Tests

4 smoke tests marked `@pytest.mark.integration` + `@pytest.mark.external` - skipped by default. Run with:

```bash
pytest -m integration
```

These verify the MCP server (port 8010) and agent card endpoints (ports 8002-8004) are reachable. Requires all services running (`run_adk_web.bat`).

## Report Generation Regression Tests (v1.39)

3 new test files validate all three output formats against realistic data patterns. All use `unittest.mock.patch("yfinance.Ticker", ...)` to avoid real network calls.

### Running

```bash
pytest src/tests/regression/ -v
```

### DOCX (`test_docx_regression.py`)

Tests `generate_docx()` with the shared `_extract_deck_data()` extraction pipeline. Validates output is valid `BytesIO`, non-empty, and handles edge cases (empty brief, unknown ticker, unicode, markdown tables).

### HTML (`test_html_regression.py`)

Tests `generate_html()` - verifies HTML structure (`<section>`, `</deck-stage>`), company name in title, deck-stage.js embedded inline (no external `src=`), CSS custom properties present, unicode encoding, and XSS prevention (`<script>` tags escaped as `&lt;script&gt;`).

### PPTX (`test_pptx_regression.py`)

Tests `generate_pptx()` - slide count verification via `_count_slides()` (parses PPTX zip structure for `ppt/slides/slide*.xml`). Validates >=6 slides for realistic data, >=3 slides for empty brief, handles long summaries without overflow.

## Characterization Tests (Phase 0)

4 test files (45 tests) providing golden regression and contract verification:

| File | Scope |
|---|---|
| `test_api_contracts.py` | ASGI in-process API route tests with temp SQLite DB and pre-stubbed ADK/a2a-sdk modules. Covers all REST endpoints with auth on/off states. |
| `test_deck_extraction_golden.py` | Golden tests over 4 fixtures (minimal/structured/empty/realistic_quant). Goldens written on first run via `UPDATE_GOLDENS=1`. Includes P1-regression test documenting current bug. |
| `test_mcp_tool_shapes.py` | MCP tool return-shape tests with mocked yfinance/feedparser. No network required. Validates each tool's response schema. |
| `test_quant_nodes_io.py` | Per-node state-in/state-out with stubbed MCP client. Each LangGraph node tested in isolation with synthetic inputs. |

## Corpus Regression Tests (Phase R)

`test_corpus_invariants.py` (126 lines) provides invariant testing across 7 corpus fixtures:

| Fixture | Purpose |
|---|---|
| `hostile.json` | Malformed/malicious LLM output |
| `one_liner.json` | Single-line LLM responses |
| `quant_heavy.json` | Mostly quantitative data, little prose |
| `sentiment_heavy.json` | Mostly narrative, few metrics |
| `table_free_prose.json` | No markdown tables, pure prose |
| `table_heavy.json` | Many markdown tables |
| `unicode_long.json` | Long text with unicode characters |

Fixtures driven: adding a new corpus JSON file automatically creates test cases via parametrization.

## Auth & Contract Tests (Phase 2 & 3)

7 test files covering authentication, authorization, and protocol contracts:

| File | Lines | Scope |
|---|---|---|
| `test_auth_tokens.py` | 132 | JWT generation, validation, expiry, rotation, signature verification |
| `test_auth_middleware.py` | 189 | Middleware chain, path exemptions, principal-kind routing, auth-off mode |
| `test_auth_routes.py` | 145 | Login/refresh/logout cycle, rate-limited lockout, cookie signing |
| `test_auth_audit.py` | 41 | Structured auth.denied logging patterns |
| `test_user_store.py` | 146 | Argon2 hashing, user CRUD, refresh token rotation |
| `test_auth_contract.py` | 170 | Parametrized auth x route matrix: every REST route x {auth on/off} x {none, user, service, admin} |
| `test_a2a_protocol.py` | 167 | In-process GenericAgentExecutor lifecycle: WORKING->artifact->COMPLETED, FAILED, CANCELED, INPUT_REQUIRED, structured data artifacts |
| `test_openapi_spec.py` | 73 | Spec regeneration check: verifies `docs/openapi.json` matches generated output |

## Test Configuration

Defined in `pyproject.toml`:
- `asyncio_mode = "auto"`
- `asyncio_default_fixture_loop_scope = "function"`
- Custom markers: `integration`, `external`, `auth`, `openapi`
