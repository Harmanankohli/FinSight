# Test Coverage

## Summary

**72/72 tests passing — 100%**

## Test Files

| File | Tests | Area |
|---|---|---|---|
| `test_a2a_communication.py` | 3 | SubAgentClient discovery, registration, unknown agent |
| `test_agent_cards.py` | 6 | Declarative JSON agent card validation |
| `test_base_agent.py` | 2 | BaseAgent abstract class and stream contract |
| `test_orchestrator_tools.py` | 4 | Orchestrator `send_message` tool, sub-agent client presence |
| `test_planner.py` | 5 | SubAgentClient, agent tool generation, send_message |
| `test_workflow.py` | 8 | WorkflowGraph nodes, edges, state transitions |
| `test_quant_graph.py` | 4 | LangGraph conditional branching, stress test/DCF routing |
| `test_rag_pipeline.py` | 7 | Hybrid search (BM25 + dense), RRF merge, document ingestion |
| `test_sentiment_crew.py` | 3 | MCP tool discovery, crew builder |
| `test_trace_propagation.py` | 14 | Trace context inject/extract (8), portfolio holdings extraction (6) |
| `test_memory.py` | 16 | Memory layer: SQLite store, TickerMemory, PortfolioStore, PerformanceTracker, SQLiteMemoryService |
| **Total** | **72** | |

## Running Tests

```bash
# All tests
uv run pytest -v

# Specific module
uv run pytest tests/test_planner.py -v

# Trace propagation only
uv run pytest tests/test_trace_propagation.py -v

# With timeout (prevents hanging on network calls)
uv run pytest -v --timeout=30
```

## Key Test Patterns

| Pattern | Test | What it verifies |
|---|---|---|
| Sync discovery | `test_sync_discovery_no_seed_urls` | Empty seed URLs → no agents |
| Card registration | `test_register_card_sync_stores_metadata` | Skill metadata persisted correctly |
| Async send | `test_send_message_unknown_agent` | Unknown agent → error message |
| Tool function | `test_agent_tool_calls_send_message` | Generated tools route to correct agent |
| Agent cards | `test_all_cards_have_urls` | All JSON cards well-formed |
| Quant graph | `test_graph_routes_to_stress_test_on_high_volatility` | Conditional branching logic |
| RAG pipeline | `test_rrf_merge_deduplicates` | BM25 + dense hybrid search |
| Trace context | `test_inject_and_extract_roundtrip` | JSON prefix inject/extract preserves trace IDs |
| Trace IDs | `test_extract_trace_ids_roundtrip` | `extract_trace_ids()` returns `(trace_id, parent_span_id, clean_query)` |
| Holdings extraction | `test_extract_holdings_portfolio_holds` | "My portfolio holds AAPL, MSFT, GOOGL" → `["AAPL", "MSFT", "GOOGL"]` |

## Trace Propagation Tests

| Test | What it verifies |
|---|---|
| `test_inject_and_extract_roundtrip` | Full inject → extract cycle preserves trace_id, parent_span_id, and original task text |
| `test_extract_no_context_is_noop` | Task without prefix passes through unchanged |
| `test_injected_task_preserves_multiline` | Multi-line task text preserved after roundtrip |
| `test_malformed_prefix_returns_none` | Invalid JSON prefix → no context extracted, original text returned |
| `test_missing_trace_fields_returns_none` | Prefix with wrong fields → returns None |
| `test_inject_empty_ids_returns_original` | Empty trace_id/parent_span_id → no injection, original text returned |
| `test_extract_trace_ids_roundtrip` | `extract_trace_ids()` returns correct `(trace_id, parent_span_id, clean_query)` tuple |
| `test_extract_trace_ids_no_context` | `extract_trace_ids()` returns `(None, None, original)` when no prefix present |

## Holdings Extraction Tests

| Test | What it verifies |
|---|---|
| `test_extract_holdings_portfolio_holds` | "My portfolio holds AAPL, MSFT, GOOGL" → `["AAPL", "MSFT", "GOOGL"]` |
| `test_extract_holdings_colon_syntax` | "My portfolio: TSLA, AMZN, META" → `["TSLA", "AMZN", "META"]` |
| `test_extract_holdings_and_connector` | "I own MSFT and GOOGL" → `["MSFT", "GOOGL"]` |
| `test_extract_holdings_no_holdings_mentioned` | "Should I buy NVDA?" → `[]` |
| `test_extract_holdings_excludes_target_ticker` | Target ticker excluded from holdings list |
| `test_extract_holdings_current_positions` | "My current holdings are JPM, BAC, WFC" → `["JPM", "BAC", "WFC"]` |

## Memory Layer Tests

| Test | What it verifies |
|---|---|
| `test_sqlite_store_creates_tables` | SQLiteStore initializes with all 6 tables |
| `test_sqlite_store_connection_reusable` | Connection is reusable and thread-safe |
| `test_ticker_memory_store_and_get_latest` | Store brief → retrieve latest recommendation |
| `test_ticker_memory_get_all_by_ticker` | Multiple briefs per ticker, ordered by date |
| `test_ticker_memory_format_context_empty` | No history → empty context string |
| `test_ticker_memory_format_context_with_history` | History → formatted context with ticker, rec, confidence |
| `test_portfolio_store_update_and_get` | Store holdings → retrieve profile |
| `test_portfolio_store_merge_holdings` | New holdings merged with existing, no duplicates |
| `test_performance_tracker_record_and_get` | Record recommendation → retrieve stats |
| `test_performance_tracker_evaluate_recommendation` | Evaluate past BUY rec against current price |
| `test_memory_service_add_and_load` | SQLiteMemoryService: add session → load memory |
| `test_memory_service_search_by_query` | Search memory entries by query string |
| `test_memory_service_format_context` | Format memory entries for prompt injection |
| `test_memory_service_duplicate_handling` | Duplicate content not stored twice |
| `test_memory_service_session_isolation` | Memory entries isolated by session_id |
| `test_memory_service_empty_search` | No matches → empty result |

## Pre-existing Test Issues

`test_rag_pipeline.py` tests require the full llama-index stack (ChromaDB, sentence-transformers, etc.).
