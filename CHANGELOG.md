# Changelog

## v0.3 — Local LLMs with Ollama

- **RAG Agent**: Switched from Groq to Ollama (llama3.2) via `llama-index-llms-ollama`
- **Quant Agent**: Added LLM summary node using `langchain-ollama` for natural language explanations
- **Sentiment Agent**: Parallel MCP data collection via `asyncio.gather` — eliminates sequential tool calls
- **Sentiment Agent**: Reduced from 4 agents to 2 (analysis + synthesis) for faster execution
- **ADK Web**: Switched to `openai/` prefix with `OPENAI_API_BASE` pointing to Ollama (fixes `LiteLlm` serialization issues)
- **Centralized config**: `shared/config.py` with env vars for both Ollama and Groq
- **MCP docstrings**: All MCP servers updated with proper Args/Returns documentation
- **DynamicMCPTool**: `args_schema` uses `str` types to handle LLM string coercion

### Breaking Changes
- RAG, Quant, and Sentiment agents now default to Ollama (local). Groq can be re-enabled by changing the model config.

## v0.2 — Official A2A SDK Integration

- **AgentExecutor pattern**: All 3 agents rewritten using `DefaultRequestHandler` + `AgentExecutor` + `InMemoryTaskStore` (from a2a-samples)
- **A2A client**: Replaced custom HTTP client with SDK's `create_client()` + `A2ACardResolver`
- **Dynamic A2A discovery**: Orchestrator discovers agents via `/.well-known/agent-card.json` on startup
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
