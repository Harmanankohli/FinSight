# Test Coverage

## Summary

**41/42 tests passing** — 97.6% pass rate. 1 pre-existing failure in Sentiment CrewAI (expects 4 agents, crew has 2).

## Test Files

| File | Tests | Coverage | Area |
|---|---|---|---|
| `test_a2a_communication.py` | 7 | 100% | A2A discovery, client, intent parsing, report synthesis |
| `test_agent_cards.py` | 6 | 100% | Declarative JSON agent card validation |
| `test_base_agent.py` | 2 | 100% | BaseAgent abstract class and stream contract |
| `test_planner.py` | 5 | 100% | Ticker extraction, risk/horizon parsing, task list generation |
| `test_workflow.py` | 7 | 100% | WorkflowGraph nodes, edges, state transitions |
| `test_quant_graph.py` | 4 | 100% | LangGraph conditional branching, stress test/DCF routing |
| `test_rag_pipeline.py` | 6 | 100% | Hybrid search (BM25 + dense), RRF merge, document ingestion |
| `test_sentiment_crew.py` | 3 | 66% | MCP tool discovery (pass), crew builder (1 fail — 4 vs 2 agents) |

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

Tests are designed to run locally without external dependencies:
- A2A tests mock HTTP clients
- RAG tests use in-memory data
- Quant tests use synthetic market data
- Agent card tests validate JSON schema only
- Sentiment test failure is known (CrewAI version mismatch)

## Key Test Patterns

| Pattern | Example |
|---|---|
| Async agent testing | `test_mock_agent_stream` — verifies `BaseAgent.stream()` yields correct dicts |
| Workflow state machine | `test_run_workflow` — validates node traversal and completion |
| Skill-based routing | `test_find_agent_by_query` — tests keyword matching logic |
| Card validation | `test_all_cards_have_urls` — ensures all agent cards are well-formed |
| Planner decomposition | `test_plan_creates_task_list` — verifies query → ordered tasks |
