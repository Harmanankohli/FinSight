# Test Coverage

## Summary

**39/39 tests passing — 100%**

## Test Files

| File | Tests | Area |
|---|---|---|
| `test_a2a_communication.py` | 3 | SubAgentClient discovery, registration, unknown agent |
| `test_agent_cards.py` | 6 | Declarative JSON agent card validation |
| `test_base_agent.py` | 2 | BaseAgent abstract class and stream contract |
| `test_planner.py` | 6 | SubAgentClient, agent tool generation, send_message |
| `test_workflow.py` | 7 | WorkflowGraph nodes, edges, state transitions |
| `test_quant_graph.py` | 4 | LangGraph conditional branching, stress test/DCF routing |
| `test_rag_pipeline.py` | 6 | Hybrid search (BM25 + dense), RRF merge, document ingestion |
| `test_sentiment_crew.py` | 3 | MCP tool discovery, crew builder |
| **Total** | **39** | |

## Running Tests

```bash
# All tests
uv run pytest -v

# Specific module
uv run pytest tests/test_planner.py -v

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

## Pre-existing Test Issues

`test_rag_pipeline.py` tests require the full llama-index stack. The `ThinkingBlock` import error in newer `llama-index-llms-ollama` versions was fixed by pinning to `<0.6.0`.
