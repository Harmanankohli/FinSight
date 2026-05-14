# Design Decisions

## Why Four Different Agent Frameworks?

Using a single framework (e.g., just CrewAI or just LangGraph) would have been simpler. The bet here is that each framework has genuine strengths, and combining them demonstrates multi-framework mastery — a key signal for roles involving agent orchestration.

| Agent | Framework | Why |
|---|---|---|
| Orchestrator | **Google ADK** | ADK has first-class A2A protocol support built-in. It handles agent card generation, A2A JSON-RPC routing, and session management natively. |
| RAG | **LlamaIndex** | LlamaIndex has the best document indexing/retrieval abstractions. Hybrid search (BM25 + dense), multi-index routing, and citation management are all first-class features. |
| Quant | **LangGraph** | The conditional state machine pattern (high volatility → stress test, low volatility → DCF) maps naturally to LangGraph's graph-based architecture. Stateful execution with checkpointing is also useful here. |
| Sentiment | **CrewAI** | Multi-agent role-playing (social analyst, insider monitor, synthesis) is what CrewAI was designed for. The "crew" abstraction makes role definitions explicit and readable. |

Key insight: this heterogeneity is also the hardest part to maintain. Each framework has its own patterns for error handling, observability, and dependency management.

## A2A Protocol v1.0 vs Custom HTTP

We use the official Google A2A SDK (`a2a-sdk`) for all inter-agent communication. The initial prototype used raw HTTP POST requests, but the SDK's `DefaultRequestHandler` + `AgentExecutor` pattern handles task lifecycle management (SUBMITTED → WORKING → COMPLETED/FAILED) properly, including streaming via SSE.

### What we learned (the hard way)

1. **messageId is required**: A2A v1.0 requires a `messageId` field on every `Message` object. Missing it causes a 400 validation error.
2. **agentInterface must match**: The `ClientFactory` requires `supported_interfaces` with `protocol_binding="JSONRPC"` on the agent card. Without this, the SDK client throws "no compatible transports found."
3. **Non-streaming vs streaming**: The `DefaultRequestHandler` returns the initial task immediately (state=WORKING) for non-streaming requests. Clients must either poll with `GetTask` or use SSE streaming to receive the final COMPLETED state.
4. **Event field names**: Streaming events use `status_update` and `artifact_update` fields (not `task`). Our initial event handler missed this, causing empty `{}` responses for hours of debugging.

## MCP for Tool Access

MCP tools are dynamically discovered. On connect, the client calls `list_tools()` on each MCP server, then routes any `call_tool_by_name()` call to the correct server automatically. This means agents don't need to know which server hosts a tool — they just call by name.

### Tool parameter types

MCP tools with `int` or `list` parameters caused issues with LLM function calling. The CrewAI `DynamicMCPTool` was updated to accept all parameters as `str` types to avoid schema validation errors when the LLM sends `"limit": "10"` instead of `"limit": 10`.

## Ollama vs Groq

The initial implementation used Groq (free tier via API) for all LLM calls. After hitting daily rate limits (100K tokens/day), we switched to local inference via Ollama.

| Factor | Groq | Ollama (local) |
|---|---|---|
| Speed | ~200 t/s | ~5-10 t/s |
| Latency | ~1-2s per call | ~10-30s per call |
| Rate limits | 6K TPM / 100K TPD | None |
| Cost | Free tier, need upgrade | Free, just hardware |
| Setup | API key only | Download 2GB model |

The tradeoff: Groq is faster but rate-limited. Ollama is slower (especially on CPU) but has no limits.

### Sentiment agent performance

The Sentiment agent originally used 4 sequential CrewAI agents making individual tool calls. This caused two problems with Groq: (a) 4+ sequential LLM calls exceeded the 6K TPM limit, and (b) the agent hallucinated tool names (`brave_search`).

**Fix**: Pre-collect MCP data in parallel using `asyncio.gather`, then pass it as context to the CrewAI agents. The agents no longer need tools — they just synthesize the provided data. This reduced LLM calls from 4+ to 2, eliminated tool hallucinations, and made the workflow faster overall.

## LlamaIndex Router Failed

The `RouterQueryEngine` with `LLMMultiSelector` kept failing because the Groq model returned malformed JSON for the routing decision. The fix was a fallback: if the router fails, query the SEC filings index directly.

## What Didn't Work

1. **Google ADK `LiteLlm` wrapper**: Caused `PydanticSerializationError: Unable to serialize unknown type`. Switched to plain model string with `openai/` prefix.
2. **Streaming with raw HTTP**: The SDK client with streaming required complex event handling. Switched to direct HTTP POST for the ADK Web agent's tool functions.
3. **CrewAI hosted_vllm provider**: The `VLLM_API_KEY` / `VLLM_BASE_URL` env vars were never properly read by CrewAI for the Groq integration. Switched to the `groq/` prefix with litellm.
4. **DefaultRequestHandlerV2**: The custom handler approach was replaced by the SDK's built-in `DefaultRequestHandler` + `AgentExecutor` pattern, which handles task lifecycle and streaming properly.
