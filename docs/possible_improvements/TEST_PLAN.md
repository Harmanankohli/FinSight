# Test Plan

Comprehensive testing strategy for the multi-agent investment system.
Covers unit, integration, end-to-end, evaluation, and regression tests.

**Current state**: 0 test files, pytest configured in `pyproject.toml` with
`pytest-asyncio` and `pytest-cov` available. Only `tests/evaluation/` exists
with 3 JSON trace artifacts.

**Prerequisite fix**: Import-time side effects (OpenTelemetry, HF_HUB_OFFLINE,
Langfuse) must be isolated before any test can run. See Improvement #9 in
ARCHITECTURE.md. Until fixed, tests must use `monkeypatch` or env var cleanup.

---

## 0. Foundation: Conftest & Fixtures

A `tests/conftest.py` with shared fixtures eliminates duplication across
all test files.

```python
# tests/conftest.py (proposed structure)

import os, pytest
from pathlib import Path

# ── Isolate import-time side effects ────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Prevent HF_HUB_OFFLINE, OTel instrumentors, etc. from firing."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("EVAL_TRACE_ENABLED", "false")
    yield

# ── In-memory SQLite fixture for memory tests ───────────────────────────
@pytest.fixture
async def memory_db(tmp_path):
    """Return a fresh in-memory SQLite connection with schema applied."""
    import aiosqlite
    from shared.memory.store import init_db
    conn = await aiosqlite.connect(":memory:")
    await init_db(conn)
    yield conn
    await conn.close()

# ── Mock MCP client fixture ─────────────────────────────────────────────
@pytest.fixture
def mock_mcp(mocker):
    """Return a mock MCPClient with canned responses."""
    from shared.mcp_client import MCPClient, MCPServerConfig
    mcp = mocker.AsyncMock(spec=MCPClient)
    mcp.call_tool_by_name = mocker.AsyncMock()
    mcp.connect_all = mocker.AsyncMock()
    mcp.disconnect_all = mocker.AsyncMock()
    return mcp

# ── Sample data ─────────────────────────────────────────────────────────
@pytest.fixture
def sample_query_context():
    from shared.models import QueryContext
    from datetime import datetime
    return QueryContext(
        ticker="AAPL",
        user_query="Analyze Apple stock",
        user_risk_profile="moderate",
        portfolio_holdings=["MSFT", "GOOGL"],
        investment_horizon="long_term",
        session_id="test-session-001",
        timestamp=datetime.now(),
    )

@pytest.fixture
def sample_brief(sample_query_context):
    from shared.models import InvestmentBrief, RAGInsights, QuantMetrics, SentimentIntelligence
    from datetime import datetime
    return InvestmentBrief(
        ticker="AAPL",
        generated_at=datetime.now(),
        query_context=sample_query_context,
        rag_insights=RAGInsights(
            ticker="AAPL", revenue_growth_yoy=0.08, rd_spend_billions=30.0,
            forward_guidance="Positive", key_risks=["Regulation"],
            cited_documents=["10-K"], confidence_score=0.85,
        ),
        quant_metrics=QuantMetrics(
            ticker="AAPL", sharpe_ratio=1.5, annual_volatility=0.25, beta=1.2,
            var_95_daily=0.02, portfolio_correlation={},
            quant_signal="BUY", quant_confidence=0.75,
        ),
        sentiment_intelligence=SentimentIntelligence(
            ticker="AAPL", social_sentiment_score=0.6, analyst_consensus="Buy",
            avg_price_target=200.0, insider_signal="Neutral",
            narrative="Strong ecosystem", overall_signal="BUY",
            confidence_score=0.8, key_risks=[], key_catalysts=[],
        ),
        final_recommendation="BUY",
        recommendation_rationale="Strong fundamentals",
        confidence_score=0.85,
        disclaimer="This is not financial advice",
    )
```

---

## 1. Unit Tests — Pure Functions (No Mocking Required)

### 1.1 `test_ticker_utils.py` — 10-15 tests

| Test | Input | Expected |
|------|-------|----------|
| Valid ticker formats | `"AAPL"`, `"BRK.B"`, `"V"`, `"GOOGL"` | `True` |
| Invalid ticker formats | `"A"*6`, `"12345"`, `"A.B.C"`, `""`, `"ABCDEF"` | `False` |
| Stop word rejection | `"SEC"`, `"EPS"`, `"CEO"`, `"AI"` | `is_valid_ticker_format` returns True but `_is_financial_stop_word` returns True |
| Ticker extraction (parens) | `"buy Visa (V)"` | `"V"` |
| Ticker extraction (mixed-case parens) | `"V (Visa)"` | `"V"` |
| Ticker extraction (preposition) | `"invest in NVDA"` | `"NVDA"` |
| Ticker extraction ($ prefix) | `"what about $TSLA"` | `"TSLA"` |
| Ticker extraction (isolated uppercase) | `"analyze AAPL"` | `"AAPL"` |
| Ticker extraction (stop word) | `"what is EPS"` | `""` |
| Ticker extraction (no ticker) | `"how are you"` | `""` |
| Ticker extraction (multi-word) | `"portfolio: AAPL, MSFT and GOOGL"` | `"AAPL"` |
| Clean query | `"analyze the stock AAPL"` | `"AAPL"` |
| Holdings extraction | `"my portfolio: AAPL, MSFT, GOOGL"` | `["AAPL", "MSFT", "GOOGL"]` |
| Holdings extraction (exclude) | `"portfolio: AAPL, MSFT"` exclude `"MSFT"` | `["AAPL"]` |
| Holdings extraction (empty) | `"no tickers here"` | `[]` |

### 1.2 `test_trace_context.py` — 8-10 tests

| Test | Input | Expected |
|------|-------|----------|
| Inject trace context | `("hello", "t1", "s1")` | `'{"_trace":{...}}\n<<<TASK>>>\nhello'` |
| Skip inject on empty ids | `("hello", "", "s1")` | `"hello"` |
| Skip inject on None | `("hello", "t1", "")` | `"hello"` |
| Extract valid context | text with separator + valid JSON | parsed dict + clean text |
| Extract no context | `"hello world"` | `(None, "hello world")` |
| Extract malformed JSON | `"not-json<<<TASK>>>\nhello"` | `(None, full string)` |
| Extract missing fields | `{"_trace": {}}` | `(None, clean text)` |
| Extract trace IDs | valid trace text | `("t1", "s1", clean_text)` |
| Round-trip | inject -> extract | original trace ids + clean text |

### 1.3 `test_models.py` — 8-12 tests

| Test | Focus |
|------|-------|
| QueryContext creation and defaults | All required fields present |
| QueryContext validation failure | Missing required field -> pydantic error |
| RAGInsights serialization round-trip | `model_dump()` -> `model_validate()` |
| QuantMetrics optional fields | `dcf_intrinsic_value=None`, `stress_test_result=None` |
| InvestmentBrief nesting | All sub-models correctly serialized |
| SentimentIntelligence defaults | `key_risks`, `key_catalysts` as empty lists |
| ISO datetime serialization | `generated_at` round-trips correctly |
| Model JSON schema generation | Schema matches expected structure |

### 1.4 `test_config.py` — 5-6 tests

| Test | Approach |
|------|----------|
| Default values | Import config with clean env, check defaults |
| Env var overrides | Set env vars before import, check values |
| Validate pass | All required vars present -> no error |
| Validate warn | Placeholder Langfuse key -> warning logged |
| Validate fail | `MCP_SERVER_URL=""` -> raises `EnvironmentError` |
| `validate()` with missing `.env` | No crash when `.env` absent |

### 1.5 `test_runtime_eval.py` — 5 tests (pure parts only)

| Test | Input | Expected |
|------|-------|----------|
| `_build_quant_reference` with metrics | `{"metrics": {"sharpe_ratio": 1.5, ...}}` | String containing all metric values |
| `_build_quant_reference` with DCF | `{"dcf_valuation": {"intrinsic_value": 150, ...}}` | String containing DCF values |
| `_build_quant_reference` with stress test | `{"stress_test": {"cvar_95": -0.03}}` | String containing CVaR |
| `_build_quant_reference` empty | `{}` | Empty string |
| `_build_quant_reference` partial | `{"metrics": {"sharpe_ratio": 1.5}}` | String with only available fields |

### 1.6 `test_logging_config.py` — 4-5 tests

| Test | Approach |
|------|----------|
| File handler created | `setup_file_logging("test")` -> file exists |
| Stream handler added | Handler of type `StreamHandler` on root logger |
| Idempotent calls | Second call doesn't duplicate handlers |
| Custom log level | `setup_file_logging("test", logging.DEBUG)` -> root level DEBUG |
| Default level | No level arg -> `logging.INFO` |

---

## 2. Unit Tests — With Mocking

### 2.1 `test_mcp_client.py` — 10-15 tests

| Test | Approach |
|------|----------|
| `parse_mcp_result(None)` | Returns `{"error": "MCP result is None"}` |
| `parse_mcp_result(dict)` | Returns dict unchanged |
| `parse_mcp_result(ListResult)` | Mock `result.content` with TextContent items |
| `parse_mcp_result(DataResult)` | Mock `result.content` with data items |
| `parse_mcp_result(JSONResult)` | Mock with `json_result` attribute |
| `parse_mcp_result(empty list)` | Returns `{"error": "No content in MCP result"}` |
| `parse_mcp_result(mixed types)` | First valid JSON is returned |
| MCPClient.connect_all failure | Mock `_connect_server` raises -> `MCPClientError` |
| MCPClient retry logic | First 2 attempts fail, 3rd succeeds |
| MCPClient.call_tool unknown server | Raises `MCPClientError` with available tools |
| MCPClient.call_tool_by_name routing | Tool registered on server A -> calls server A |
| MCPClient.disconnect_all | Mocks session/transport cleanup |
| MCPClient._load_config | YAML with mcp_servers list -> correct MCPServerConfig objects |

### 2.2 `test_generic_executor.py` — 8-10 tests

| Test | Approach |
|------|----------|
| `execute` completes successfully | Mock agent.stream yields `{is_task_complete: True, content: "ok"}` -> COMPLETED |
| `execute` with error | Mock agent.stream yields `{is_task_complete: True, is_error: True}` -> FAILED |
| `execute` requires user input | Mock agent.stream yields `{require_user_input: True}` -> input_required |
| `execute` streaming progress | Mock yields multiple progress items -> WORKING events emitted |
| `execute` structured data | Mock yields `{response_type: "data", content: dict}` -> protobuf Struct |
| `execute` with no task | Empty context -> new task created |
| `cancel` | Raises `Exception("cancel not supported")` |
| `execute` unexpected exception | Agent.stream raises -> handled gracefully |

### 2.3 `test_semantic_cache.py` — 5-6 tests

| Test | Approach |
|------|----------|
| `get` cache hit | Mock embedder + ChromaDB return high similarity -> returns response |
| `get` cache miss (low sim) | Mock returns low similarity -> returns None |
| `get` cache miss (expired) | Mock returns high sim but old timestamp -> returns None |
| `set` stores entry | Mock embedder + ChromaDB -> `chroma.add` called with correct args |
| `get` with unavailable backend | `chromadb` import fails -> returns None gracefully |
| `set` with unavailable backend | `chromadb` import fails -> no crash |

### 2.4 `test_memory_store.py` — 8-10 tests

| Test | Approach |
|------|----------|
| `init_db` creates tables | In-memory SQLite -> all 5 tables exist |
| `init_db` idempotent | Second call -> no error |
| Schema migration | `SCHEMA_VERSION` mismatch -> migration runs |
| `is_filing_ingested` hit | Pre-inserted URL returns True |
| `is_filing_ingested` miss | Unknown URL returns False |
| `mark_filing_ingested` | Insert URL -> `is_filing_ingested` returns True |
| `mark_filing_ingested` duplicate | Same URL twice -> no error (INSERT OR IGNORE) |
| `get_db` WAL mode | New connection -> PRAGMA journal_mode returns "wal" |
| `get_db` foreign keys | New connection -> PRAGMA foreign_keys returns 1 |

### 2.5 `test_ticker_memory.py` — 8-10 tests

| Test | Approach |
|------|----------|
| `store_brief` | Store InvestmentBrief -> record_id returned, row exists |
| `store_minimal` | Store text-only -> row inserted correctly |
| `get_latest` by ticker | Returns most recent record |
| `get_latest` no results | Unknown ticker -> None |
| `get_latest` by user | Returns user-filtered results |
| `get_history` | Multiple records -> newest first, limited by N |
| `has_changed` different | Two recs with different recommendations -> `{changed: True}` |
| `has_changed` same | Two recs with same -> `{changed: False}` |
| `has_changed` insufficient | One record -> None |
| `format_context` | Returns compact string with rec, confidence, date |
| `update_response_text` | Update existing record -> success |
| `update_response_text` not found | Unknown ID -> False |

### 2.6 `test_portfolio_store.py` — 5-6 tests

| Test | Approach |
|------|----------|
| `get_profile` found | Existing user -> full profile |
| `get_profile` not found | Unknown user -> None |
| `upsert_from_context` new | First query -> row created |
| `upsert_from_context` merge | Second query with new holdings -> merged |
| `get_holdings` | Returns list from profile |
| `update_holdings` | Explicit set -> stored correctly |

### 2.7 `test_performance_tracker.py` — 6-7 tests

| Test | Approach |
|------|----------|
| `record_recommendation` with price | Insert with price -> row exists |
| `record_recommendation` without price | Mock yfinance -> price fetched, row exists |
| `evaluate_all` | Mock yfinance returns current price -> returns correct realized_return |
| `evaluate_all` no unevaluated | All evaluated -> empty list |
| `get_accuracy_stats` | BUY correct (ret>0), SELL correct (ret<0), HOLD -> correct rates |
| `get_accuracy_stats` by user | Filtered by user_id |
| `get_past_recommendations` | Date-filtered results |

### 2.8 `test_sub_agent_client.py` — 8-10 tests

| Test | Approach |
|------|----------|
| `discover` success | Mock HTTPX returns valid A2ACard -> agent registered |
| `discover` retry | First attempt fails, second succeeds -> agent registered |
| `discover` all fail | All retries fail -> empty agents dict, warning logged |
| `resolve_agent_name` exact | "RAG Agent" -> match |
| `resolve_agent_name` case-insensitive | "rag agent" -> match |
| `resolve_agent_name` fuzzy | "RAG" in "RAG Agent" -> match |
| `resolve_agent_name` not found | "Unknown" -> None |
| `send_message` success | Mock A2A client returns completed -> text response |
| `send_message` error | Mock returns failed state -> error JSON |
| `send_message` timeout | Client raises -> error JSON |

### 2.9 `test_observability.py` — 4-5 tests

| Test | Approach |
|------|----------|
| `init_langfuse` singleton | Second call returns same client |
| `get_langfuse_client` lazy | Called before init -> auto-initializes |
| `flush_langfuse` not initialized | No error |
| `shutdown_langfuse` | Calls flush + shutdown |
| `init_langfuse` OTLP env vars | Sets `OTEL_EXPORTER_OTLP_ENDPOINT` and headers |

---

## 3. Integration Tests

### 3.1 `test_mcp_server_integration.py` — 5-6 tests

Uses `TestClient` against the Starlette app from `mcp_servers/finsight_server.py`.

| Test | Approach |
|------|----------|
| Health endpoint | `GET /health` -> `200` |
| Agent card resource | `GET /resource://agent_cards/list` -> valid JSON |
| Agent card by name | `GET /resource://agent_cards/orchestrator_agent.json` -> valid card |
| MCP tool `validate_ticker` known | Call with "AAPL" -> `{"valid": true, ...}` (requires SEC) |
| MCP tool `validate_ticker` unknown | Call with "ZZZZZ" -> `{"valid": false}` |
| MCP tool `execute_python` safe | `"print('hello')"` -> result |
| MCP tool `execute_python` blocked | `"import os; os.system('rm')"` -> error or restricted |

**Note**: These tests depend on SEC EDGAR network access and LM Studio.
They should be marked with `@pytest.mark.external` and skipped when
network is unavailable by default.

### 3.2 `test_a2a_protocol_integration.py` — 4-5 tests

Tests the A2A protocol layer on each agent server.

| Test | Approach |
|------|----------|
| A2A card endpoint | `GET /.well-known/agent-card.json` on port 8002 -> 200 + valid card |
| A2A health endpoint | `GET /health` on ports 8001-8004, 8010 -> 200 |
| A2A `send_message` (sub-agent) | POST to orchestrator -> valid A2A response |
| Sub-agent discovery round-trip | Orchestrator discovers all sub-agents from seed URLs |
| A2A `send_message` with trace context | Injected trace survives round-trip -> clean task text on sub-agent side |

**Note**: Requires all 5 servers running (docker-compose up). Mark with
`@pytest.mark.docker` or `@pytest.mark.integration`.

### 3.3 `test_memory_integration.py` — 4-5 tests

Tests the SQLite memory layer with real aiosqlite connections.

| Test | Approach |
|------|----------|
| Full ticker_memory workflow | `store_brief` -> `get_latest` -> `get_history` -> `has_changed` |
| Full portfolio_store workflow | `upsert_from_context` -> `get_profile` -> `get_holdings` -> `update_holdings` |
| Filing dedup workflow | `mark_filing_ingested` -> `is_filing_ingested` (True) -> different URL (False) |
| Concurrent read/write | Two concurrent writes to different tickers -> no SQLITE_BUSY |
| Schema migration on old DB | Create tables without search_text -> `init_db` adds it |

### 3.4 `test_ticker_flow_integration.py` — 3-4 tests

Tests the ticker extraction -> validation -> resolution pipeline
(start-to-finish through `shared/ticker_utils.py` with real/mocked MCP).

| Test | Approach |
|------|----------|
| `extract_ticker` -> `validate_ticker_via_mcp` | Extract "NVDA" -> validate via mock -> returns correct |
| `clean_query_for_resolution` -> `resolve_ticker_via_mcp` | Clean name "NVIDIA" -> resolve -> ticker "NVDA" (req. mock) |
| `extract_holdings` -> validate each | Three tickers extracted -> all validated |

---

## 4. End-to-End Tests

### 4.1 `test_full_pipeline_e2e.py` — 2-3 tests

Simulates a complete user query from orchestrator input to final response,
validating the shape and content of the output at each stage.

| Test | Approach |
|------|----------|
| Happy path: single ticker | `"Analyze Apple (AAPL)"` -> all sub-agents respond -> InvestmentBrief produced |
| Happy path: with portfolio | `"How does AAPL fit my portfolio of MSFT, GOOGL?"` -> correlation included |
| Off-topic rejection | `"What's the weather?"` -> blocked by guardrail |

**Requires**: All 5 servers running + LM Studio + network. Mark with
`@pytest.mark.e2e`.

---

## 5. Evaluation Tests

### 5.1 `test_evaluation_traces.py` — 3-4 tests

Validates the existing evaluation trace artifacts and the trace format.

| Test | Approach |
|------|----------|
| Existing traces are valid JSON | Parse each file in `tests/evaluation/eval_results/orchestrator_traces/` |
| Trace structure | Each trace has `agent_name`, `task_sent`, `response`, `latency_ms` |
| Trace agent names match | `agent_name` values match known agents (RAG, Quant, Sentiment) |
| Trace has plausible latency | `latency_ms` is positive and reasonable |

### 5.2 `test_ragas_scoring.py` — 5-6 tests

Tests the RAGAS scoring functions in `shared/runtime_eval.py` with mocked
ragas clients.

| Test | Approach |
|------|----------|
| `_setup_ragas_clients` with deps | Mock `instructor`, `openai`, `ragas` -> returns (llm, embedder) |
| `_setup_ragas_clients` without deps | `ImportError` -> returns None |
| `_score_metric` success | Mock `metric.ascore` -> returns float value |
| `_score_metric` failure | Mock raises -> error logged, re-raised |
| `_push_scores` with trace_id | Mock Langfuse -> `create_score` called with correct args |
| `_push_scores` without trace_id | No trace ID -> no-op |

---

## 6. Agent-Level Tests (Unit + Integration)

### 6.1 `test_rag_agent.py` — 8-10 tests

| Test | Approach |
|------|----------|
| `RAGAgent.stream` returns valid dict | Mock MCP + index.query -> correct response shape |
| `_ensure_ingested` daily dedup | Same ticker twice on same day -> second call skips ingestion |
| `_ensure_ingested` filing dedup | Already-ingested filing URL skipped via `is_filing_ingested` |
| `stream` with no ticker | Query without ticker -> error dict |
| `stream` with invalid ticker | MCP validate returns invalid -> error with message |
| `_disconnect` | After stream, MCP client is None |
| `_validate_ticker` fallback | MCP fails -> proceeds with regex guess |
| `_build_response` with trace ctx | Extract trace ctx -> observation created with parent span |

### 6.2 `test_quant_agent.py` — 8-10 tests

| Test | Approach |
|------|----------|
| `QuantAgent.stream` returns valid dict | Mock graph.run -> correct shape |
| `stream` with no ticker | No ticker found -> error dict |
| `stream` MCP connect failure | `_ensure_connected` fails -> handled gracefully |
| `analyze` creates Langfuse handler | Mock CallbackHandler created with trace_ctx |
| Holdings extraction in stream | Extract from query -> passed to graph.run |

### 6.3 `test_quant_graph_nodes.py` — 10-15 tests

Pure computation functions - excellent unit test targets.

| Test | Approach |
|------|----------|
| `fetch_price_data_node` | Mock MCP returns price history -> state updated |
| `compute_metrics_node` | Sample price array -> correct Sharpe, beta, VaR |
| `stress_test_node` | Sample returns -> CVaR computed |
| `dcf_valuation_node` | Sample FCF data -> DCF value computed |
| `correlation_node` | Price + holdings data -> correlation matrix |
| `format_output_node` | All fields populated -> formatted dict |
| `llm_summary_node` | Mock LLM -> summary string |
| `_parse_price_data` | Raw yfinance CSV -> structured format |
| `_get_fcf_from_financials` | Financial dict -> FCF value |
| Edge: empty price data | No data -> node handles gracefully |
| Edge: single stock (no correlation) | No holdings -> correlation returns empty |

### 6.4 `test_hybrid_search.py` — 6-8 tests

| Test | Approach |
|------|----------|
| `_rrf_merge` equal weights | Two rank lists -> fused correctly |
| `_rrf_merge` empty lists | Empty sparse and dense -> empty result |
| `rrf_merge` one empty | Only sparse has items -> sparse items returned |
| `sparse_retrieve` | BM25 with sample documents -> ranked results |
| `rerank` | CrossEncoder scores -> reordered results |
| `retrieve` full pipeline | Sparse -> dense -> RRF -> rerank -> final results |

### 6.5 `test_sentiment_agent.py` — 6-8 tests

| Test | Approach |
|------|----------|
| `stream` returns valid dict | Mock MCP + CrewAI -> correct shape |
| `stream` with no ticker | No ticker -> error dict |
| `_collect_data_parallel` | Mock news + filings calls -> both results returned |
| `_collect_data_parallel` one fails | News fails, filings succeeds -> partial results |
| `_extract_sentiment_contexts` | Sample news + filings data -> flat text strings |
| `stream` MCP connect failure | `_connect` fails -> graceful degradation |

---

## 7. Security Tests

### 7.1 `test_sandbox_security.py` — 8-10 tests (critical)

Tests the `execute_python` tool in `finsight_server.py` which is a hardened
Python sandbox exposed as an MCP tool.

| Test | Approach |
|------|----------|
| `execute_python` import restriction | `import os; os.system("dir")` -> blocked |
| `execute_python` file I/O | `open("test.txt")` -> blocked |
| `execute_python` subprocess | `import subprocess` -> blocked |
| `execute_python` network | `import requests; requests.get(...)` -> blocked |
| `execute_python` simple arithmetic | `2 + 2` -> `4` |
| `execute_python` timeout | Infinite loop -> times out |
| `execute_python` max size | Huge output -> truncated |
| `execute_python` memory bomb | Large list -> OOM handled |

---

## 8. Test Organization (File Tree)

```
tests/
├── conftest.py                    # Shared fixtures
├── __init__.py
│
├── unit/
│   ├── test_ticker_utils.py
│   ├── test_trace_context.py
│   ├── test_models.py
│   ├── test_config.py
│   ├── test_logging_config.py
│   ├── test_runtime_eval.py
│   ├── test_mcp_client.py
│   ├── test_generic_executor.py
│   ├── test_semantic_cache.py
│   ├── test_observability.py
│   ├── test_sub_agent_client.py
│   │
│   └── memory/
│       ├── test_memory_store.py
│       ├── test_ticker_memory.py
│       ├── test_portfolio_store.py
│       └── test_performance_tracker.py
│
├── integration/
│   ├── test_mcp_server_integration.py    [@pytest.mark.external]
│   ├── test_a2a_protocol_integration.py  [@pytest.mark.integration]
│   ├── test_memory_integration.py
│   └── test_ticker_flow_integration.py
│
├── agents/
│   ├── test_rag_agent.py
│   ├── test_quant_agent.py
│   ├── test_quant_graph_nodes.py
│   ├── test_sentiment_agent.py
│   ├── test_orchestrator_executor.py
│   └── test_hybrid_search.py
│
├── e2e/
│   └── test_full_pipeline_e2e.py         [@pytest.mark.e2e]
│
├── evaluation/
│   ├── eval_results/
│   │   └── orchestrator_traces/
│   │       ├── d6e13...json
│   │       ├── 641e4...json
│   │       └── 461b9...json
│   └── test_ragas_scoring.py
│
└── security/
    └── test_sandbox_security.py
```

---

## 9. Test Markers & Configuration

Add to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
    "external: requires network access (SEC EDGAR, Yahoo, etc.)",
    "integration: requires multiple services running",
    "e2e: requires full pipeline (all services + LM Studio)",
    "slow: takes >10 seconds to run",
    "security: security-critical tests (sandbox escaping, etc.)",
]
```

Run targets:

```bash
# Fast unit tests only (no network, no containers)
pytest tests/unit -v --no-header -q

# All tests except slow/external
pytest tests/ -v -m "not external and not integration and not e2e"

# Full suite
pytest tests/ -v

# With coverage
pytest tests/ -v --cov=shared --cov=agent_1_adk --cov=mcp_servers
```

---

## 10. Effort Summary

| Category | Files | Tests | Effort | Estimated Lines |
|----------|-------|-------|--------|-----------------|
| Unit (pure) | 5 | 45-60 | Small | 600-800 |
| Unit (mocked) | 11 | 85-110 | Medium | 1500-2000 |
| Integration | 4 | 16-20 | Medium | 400-500 |
| Agent tests | 6 | 50-70 | Large | 1000-1500 |
| End-to-end | 1 | 2-3 | Small | 100-150 |
| Security | 1 | 8-10 | Small | 150-200 |
| Evaluation | 2 | 5-7 | Small | 100-150 |
| **Total** | **30** | **211-280** | -- | **3850-5300** |

**Estimated total effort**: 2-3 days for a first pass covering the critical
paths (unit tests for shared/ + quant graph nodes + security sandbox).

---

## 11. Implementation Order (Recommended)

1. **Phase 0** -- Conftest + env isolation (blocks everything else)
2. **Phase 1** -- Pure-function unit tests (ticker_utils, trace_context, models, config, logging) - fastest ROI
3. **Phase 2** -- Memory layer tests (store, ticker_memory, portfolio_store, performance_tracker) - uses in-memory SQLite
4. **Phase 3** -- MCP client + generic executor + semantic cache tests (needs mocking)
5. **Phase 4** -- Quant graph node pure-function tests (compute_metrics, DCF, stress_test) - numerical correctness
6. **Phase 5** -- Agent-level tests (RAG, Quant, Sentiment executors with mocked MCP)
7. **Phase 6** -- Integration tests (MCP server, ticker flow, memory)
8. **Phase 7** -- Security sandbox tests (critical, low effort)
9. **Phase 8** -- E2E + evaluation tests
