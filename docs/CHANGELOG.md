# Changelog

## v1.0 — LM Studio Migration

- **Model change**: All agents migrated from Ollama (`qwen2.5:7b`) to LM Studio (`gpt-oss-20b`) — OpenAI-compatible local API
- **Config**: Removed `OLLAMA_BASE_URL`, changed `LLM_BASE_URL` default to `http://localhost:1234/v1`
- **Dependencies**: Replaced `llama-index-llms-ollama` with `llama-index-llms-openai-like`, `langchain-ollama` with `langchain-openai`
- **Agent 3 (Quant)**: Switched from direct `yfinance` calls to MCP tools (`get_prices`, `get_financials`)
- **Agent 3 (Quant)**: Removed orphaned `mcp_config.yaml` — MCP config now sourced from `shared/config.py` with dynamic tool discovery
- **Agent 2 (RAG)**: Removed static `mcp_config.yaml` — MCP server URL passed inline via `MCPServerConfig`
- **Agent 4 (Sentiment)**: Removed static `mcp_config.yaml` — same inline pattern
- **`.env`**: Cleaned up obsolete Ollama variables
- **Orchestrator**: Updated instruction to use ADK env var `OPENAI_API_BASE`

## v0.9 — Model Migration to qwen2.5:7b

- **Model change**: All agents migrated from `llama3.2` to `qwen2.5:7b` — reliable tool calling and better instruction following
- **`.env.example`**: Updated default models to `qwen2.5:7b`
- **Docs**: All model references updated from `llama3.2` to `qwen2.5:7b`

## v0.8 — Streamlined ADK Agent

- **ADK agent restructured**: Replaced `gateway.py`, `orchestrator.py`, `a2a_client.py`, `planner.py`, `report_generator.py`, `intent_parser.py` with clean `agent.py` + `sub_agent_client.py` + `agent_executor.py` + `main.py`
- **Dynamic per-agent tools**: One ADK tool generated per discovered A2A agent — no hardcoded tool definitions
- **Sync discovery**: `SubAgentClient.discover_sync()` uses sync HTTP (no asyncio conflicts with ADK Web UI). Retries failed URLs 3x with 5s delay.
- **Lazy A2A clients**: `create_client()` called on first tool use, cached per agent
- **Proper timeout propagation**: `ClientConfig(httpx_client=h)` with 300s timeout + `ClientCallContext(timeout=300)`
- **Response extraction**: `_extract_text` handles both `text` and `data` artifact parts
- **Removed dead code**: 6 files removed, ~500 lines eliminated
- **39 tests passing**
- **Batch files**: `run_adk_web.bat` updated, `stop_servers.bat` added

## v0.7 — Return to llama3.2

- **Model change**: Returned to `llama3.2` after testing qwen3.5, lfm2.5-thinking, ministral-3, and granite4.1

## v0.6 — Model Testing Phase

- Tested models: qwen3.5, lfm2.5-thinking, ministral-3, granite4.1

## v0.5 — Qwen Model Migration

- Switched from Ollama llama3.2 to qwen3.5:0.8b
- Prompt improvements with clearer tool-call rules

## v0.4 — Reference Codebase Refactor

- MCP consolidation: 6 individual MCP servers → single `finsight_server.py`
- Declarative agent cards in `agent_cards/*.json`
- MCP-based agent registry with embedding search
- GenericAgentExecutor pattern saving ~300 lines
- Unified A2ADiscoverer + A2AClient
- WorkflowGraph state machine
- 42/42 tests passing

## v0.3 — Local LLMs with Ollama

- RAG Agent switched from Groq to Ollama (llama3.2)
- Quant Agent: added LLM summary node
- Sentiment Agent: parallel MCP data collection, reduced from 4 to 2 agents
- ADK Web: `openai/` prefix with Ollama endpoint

## v0.2 — Official A2A SDK Integration

- AgentExecutor pattern for all 3 sub-agents
- A2A client via SDK's `create_client()` + `A2ACardResolver`
- Dynamic A2A discovery
- 21 tests passing

## v0.1 — Initial Implementation

- Project scaffolding, Docker Compose
- 4 MCP servers (yfinance, SEC EDGAR, Reddit, Python Runner)
- All 4 agents with initial implementations
- 8 pytest tests
