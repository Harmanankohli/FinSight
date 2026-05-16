# Changelog

## v1.2 — Model Update & MCP Server Hardening

- **Model change**: All agents migrated from `gpt-oss-20b` to `qwen3-30b-a3b-2507` — ~5-10x faster inference per LLM call
- **`.env` / `.env.example`**: Updated `LLM_MODEL` and `ADK_MODEL` defaults to qwen

- **Windows compatibility**: `import resource` guarded by `sys.platform != "win32"` check
- **Lazy agent registry**: Model download deferred to first tool call (`_ensure_registry`), no blocking at import time
- **Thread-safe SSE app**: `get_app()` with double-checked locking (`_starlette_app` singleton)
- **Inline imports**: `import re as _re` inside `_resolve_company_keywords` and `_normalise_for_match` for localised scope
- **NaN/Inf serialisation**: `_serialise_value` handles NaN, infinity, numpy types, datetimes
- **EDGAR caching**: In-memory CIK/ticker/title map with lock-protected lazy loading
- **Expanded sandbox restricted imports**: `builtins`, `gc`, `threading`, `multiprocessing`, `signal`, `mmap`, `resource`, `pwd`, `grp`, `crypt` added to blocklist
- **SEC earnings fallback**: `get_earnings_calendar` falls back to EDGAR XBRL when yfinance lacks data
- **Retry logic**: EDGAR company filings URL fetch uses 3-attempt exponential backoff
- **42 tests passing**

## v1.1 — A2A Protocol Alignment

- **A2A discovery**: Replaced sync raw HTTP with async `A2ACardResolver` — standard `/.well-known/agent-card.json`, protobuf `AgentCard` types, backwards compatibility
- **A2A client**: Replaced `create_client()` with `ClientFactory` — proper transport negotiation, matches official A2A SDK pattern
- **Single `send_message` tool**: Removed per-agent tool generation. LLM now uses one tool with `agent_name` parameter, matching all A2A sample projects (Google, bhancockio, theailanguage)
- **Removed `list_remote_agents`**: LLM already sees agents in the instruction prompt
- **Pre-fetch removed**: `FinSightAgentExecutor` no longer pre-fetches sub-agent data. LLM routes via `send_message` tool, matching A2A sample executor pattern
- **Streaming event handling**: Correctly skips SUBMITTED/WORKING events, captures `artifact_update` (data + text parts) and terminal `status_update` events
- **Background async discovery**: Supports both ADK Web UI (running event loop → `loop.create_task()`) and CLI (`asyncio.run()`)
- **Windows event loop fix**: `WindowsSelectorEventLoopPolicy` prevents noisy `ConnectionResetError`
- **Programmatic AgentCards**: All servers now build `AgentCard` in code using protobuf types — removed `agent_1_adk/agent_card.json`
- **Agent card descriptions**: Updated from "Ollama" to "LM Studio"
- **44 tests passing** (42 standard + 2 orchestrator tool tests removed with `list_remote_agents`)

## v1.0 — LM Studio Migration

- **Model change**: All agents migrated from Ollama (`qwen2.5:7b`) to LM Studio (`gpt-oss-20b`) — OpenAI-compatible local API
- **Config**: Removed `OLLAMA_BASE_URL`, changed `LLM_BASE_URL` default to `http://localhost:1234/v1`
- **Dependencies**: Replaced `llama-index-llms-ollama` with `llama-index-llms-openai-like`, `langchain-ollama` with `langchain-openai`
- **Agent 3 (Quant)**: Switched from direct `yfinance` calls to MCP tools (`get_prices`, `get_financials`)
- **Agent 2 (RAG)**: Removed static `mcp_config.yaml` — MCP server URL passed inline via `MCPServerConfig`
- **Agent 4 (Sentiment)**: Removed static `mcp_config.yaml` — same inline pattern
- **`.env`**: Cleaned up obsolete Ollama variables

## v0.9 — Model Migration to qwen2.5:7b

- **Model change**: All agents migrated from `llama3.2` to `qwen2.5:7b`
- **`.env.example`**: Updated default models

## v0.8 — Streamlined ADK Agent

- **ADK agent restructured**: Replaced legacy modules with clean `agent.py` + `sub_agent_client.py` + `agent_executor.py` + `main.py`
- **39 tests passing**

## v0.7 — v0.1

- Earlier iterations: model testing, MCP consolidation, initial A2A SDK integration, project scaffolding
