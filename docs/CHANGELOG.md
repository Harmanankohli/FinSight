# Changelog

## v0.6 — Ministral-3 Migration

- **Model change**: Switched from qwen3.5 to ministral-3:3b for better instruction following and larger context window

## v0.5 — Qwen Model Migration

- **Model change**: Switched from Ollama llama3.2 to qwen3.5:0.8b for faster inference and better instruction following
- **Prompt improvements**: Rewrote ADK agent instructions with clearer tool-call rules
- **RAG timeout**: Increased Ollama `request_timeout` from 120s to 600s

## v0.4 — Reference Codebase Refactor

- **MCP consolidation**: 6 individual MCP servers merged into single `finsight_server.py` (port 8010) with agent registry + all data tools
- **Declarative agent cards**: Moved from Python code to `agent_cards/*.json` files loaded at runtime
- **MCP-based agent registry**: New `find_agent` tool with embedding search for semantic agent discovery
- **GenericAgentExecutor pattern**: 3 duplicated A2A executors replaced with shared `GenericAgentExecutor` + `BaseAgent` contract, saving ~300 lines
- **Consolidated A2A client**: Unified `A2ADiscoverer` + `A2AClient` with both seed URL and MCP registry discovery
- **WorkflowGraph**: Added state machine for orchestrator task execution with pause/resume support
- **Planner**: Query decomposition into ordered `TaskList` with skill-based routing
- **Singleton discoverer**: ADK agent tool calls share a single cached discoverer with `asyncio.Lock` to avoid race conditions
- **Timeout fixes**: `ClientCallContext(timeout=...)` propagated to A2A SDK transport, `.env` increased to 300s
- **42/42 tests passing** (was 21), including new tests for BaseAgent, WorkflowGraph, planner, agent cards
- **Docs**: README, ARCHITECTURE.md, AGENTS.md, TESTS.md updated

## v0.5 — Qwen Model Migration

- **Model change**: Switched from Ollama llama3.2 to qwen3.5:0.8b for faster inference and better instruction following
- **Prompt improvements**: Rewrote ADK agent instructions with clearer tool-call rules. Greetings and non-stock queries no longer trigger tool calls.

## v0.4 — Reference Codebase Refactor

...

## v0.3 — Local LLMs with Ollama

- **RAG Agent**: Switched from Groq to Ollama (llama3.2) via `llama-index-llms-ollama`
- **Quant Agent**: Added LLM summary node using `langchain-ollama` for natural language explanations
- **Sentiment Agent**: Parallel MCP data collection via `asyncio.gather` — eliminates sequential tool calls
- **Sentiment Agent**: Reduced from 4 agents to 2 (analysis + synthesis) for faster execution
- **ADK Web**: Switched to `openai/` prefix with `OPENAI_API_BASE` pointing to Ollama
- **Centralized config**: `shared/config.py` with env vars for both Ollama and Groq
- **MCP docstrings**: All MCP servers updated with proper Args/Returns documentation
- **DynamicMCPTool**: `args_schema` uses `str` types to handle LLM string coercion

## v0.2 — Official A2A SDK Integration

- **AgentExecutor pattern**: All 3 agents rewritten using `DefaultRequestHandler` + `AgentExecutor` + `InMemoryTaskStore`
- **A2A client**: Replaced custom HTTP client with SDK's `create_client()` + `A2ACardResolver`
- **Dynamic A2A discovery**: Orchestrator discovers agents via `/.well-known/agent-card.json`
- **Financial News MCP**: Replaced Reddit sentiment with free RSS-based news aggregation
- **Dynamic MCP tools**: `MCPClient.call_tool_by_name()` auto-routes by tool name
- **Gateway**: Refactored to use SDK client with `AgentInterface` negotiation
- **MCP configs**: Fixed port inconsistencies, removed unused servers
- **SEC EDGAR**: Fixed `Host` header causing 404s, changed `form_types` to comma-separated string
- **Tests**: Updated for new A2A patterns (21 tests passing)

## v0.1 — Initial Implementation

- **Project scaffolding**: Directory structure, `pyproject.toml`, Docker Compose
- **4 MCP servers**: yfinance, SEC EDGAR, Reddit, Python Runner
- **Agent 1**: ADK Orchestrator with intent parser, A2A client, report generator
- **Agent 2**: LlamaIndex RAG with ChromaDB, BM25 hybrid search, Groq LLM
- **Agent 3**: LangGraph Quant with state machine, conditional branching (stress test vs DCF)
- **Agent 4**: CrewAI Sentiment with 4-agent crew (social, analyst, insider, synthesis)
- **A2A protocol**: Custom JSON-RPC client with retry logic
- **ADK Web**: Agent discovery via agent cards with dynamic tool creation
- **Tests**: 8 pytest tests for A2A comms, RAG pipeline, LangGraph graph, CrewAI crew
- **Docker**: Docker Compose with all 4 agents + MCP servers + Redis
