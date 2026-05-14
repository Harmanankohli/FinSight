# Test Coverage

## Summary

**42/42 tests passing — 100%**

## Test Files

| File | Tests | Coverage | Area |
|---|---|---|---|
| `test_a2a_communication.py` | 7 | ✅ | A2A discovery, client, intent parsing, report synthesis |
| `test_agent_cards.py` | 6 | ✅ | Declarative JSON agent card validation |
| `test_base_agent.py` | 2 | ✅ | BaseAgent abstract class and stream contract |
| `test_planner.py` | 5 | ✅ | Ticker extraction, risk/horizon parsing, task list generation |
| `test_workflow.py` | 7 | ✅ | WorkflowGraph nodes, edges, state transitions |
| `test_quant_graph.py` | 4 | ✅ | LangGraph conditional branching, stress test/DCF routing |
| `test_rag_pipeline.py` | 6 | ✅ | Hybrid search (BM25 + dense), RRF merge, document ingestion |
| `test_sentiment_crew.py` | 3 | ✅ | MCP tool discovery, crew builder |
| **Total** | **42** | **100%** | |

## Running Tests

```bash
# All tests
uv run python -m pytest tests/ -v

# Specific module
uv run python -m pytest tests/test_a2a_communication.py -v

# With timeout (prevents hanging on network calls)
uv run python -m pytest tests/ -v --timeout=30
```

## Continuous Integration

All 42 tests run locally without external service dependencies:
- A2A tests mock HTTP clients
- RAG tests use in-memory data
- Quant tests use synthetic market data
- Agent card tests validate JSON schema only
- Sentiment tests mock CrewAI and MCP clients

## Key Test Patterns

| Pattern | Example |
|---|---|
| Async agent testing | `test_mock_agent_stream` — verifies `BaseAgent.stream()` yields correct dicts |
| Workflow state machine | `test_run_workflow` — validates node traversal and completion |
| Skill-based routing | `test_find_agent_by_query` — tests keyword matching logic |
| Card validation | `test_all_cards_have_urls` — ensures all agent cards are well-formed |
| Planner decomposition | `test_plan_creates_task_list` — verifies query → ordered tasks |
