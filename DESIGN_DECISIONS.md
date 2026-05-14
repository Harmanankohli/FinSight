# Design Decisions

## Why Four Different Agent Frameworks?

Using a single framework (e.g., just CrewAI or just LangGraph) would have been simpler. The bet here is that each framework has genuine strengths, and combining them demonstrates multi-framework mastery — a key signal for roles involving agent orchestration.

| Agent | Framework | Why |
|---|---|---|
| Orchestrator | **Google ADK** | ADK has first-class A2A protocol support built-in. Handles agent card generation, A2A JSON-RPC routing, and session management natively. |
| RAG | **LlamaIndex** | Best document indexing/retrieval abstractions. Hybrid search (BM25 + dense), multi-index routing, and citation management are first-class features. |
| Quant | **LangGraph** | Conditional state machine (high volatility → stress test, low volatility → DCF) maps naturally to LangGraph's graph-based architecture. |
| Sentiment | **CrewAI** | Multi-agent role-playing (analysis + synthesis) is what CrewAI was designed for. The "crew" abstraction makes role definitions explicit. |

## A2A Protocol v1.0 vs Custom HTTP

We use the official Google A2A SDK (`a2a-sdk`) for all inter-agent communication. The initial prototype used raw HTTP POST requests, but the SDK's `DefaultRequestHandler` + `AgentExecutor` pattern handles task lifecycle properly.

### What we learned (the hard way)

1. **messageId is required**: A2A v1.0 requires a `messageId` field on every `Message` object.
2. **agentInterface must match**: `ClientFactory` requires `supported_interfaces` with `protocol_binding="JSONRPC"`.
3. **Non-streaming vs streaming**: The handler returns the initial task immediately for non-streaming. Clients must poll or use SSE.
4. **new_text_message vs manual Message**: The old a2a-sdk requires `new_text_message(query, role=1)` instead of `Message(role=Role.ROLE_USER, parts=[...])` — the protobuf enum wrapper doesn't serialize correctly in JSON-RPC.
5. **Context timeout**: `ClientCallContext(timeout=...)` must be passed to `client.send_message(req, context=ctx)` — httpx client timeout alone is insufficient because `get_http_args()` overrides it.

## GenericAgentExecutor Pattern

Originally each agent had its own `AgentExecutor` with ~100 lines of duplicated A2A event plumbing. We extracted a shared `GenericAgentExecutor` that delegates to a `BaseAgent.stream()` contract:

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor.execute()
  → BaseAgent.stream(query, context_id, task_id)
  → Yields: {response_type, content, is_task_complete, require_user_input}
  → GenericAgentExecutor converts to A2A events
```

This eliminated ~300 lines of duplicated code and made adding new agents simpler.

## MCP Consolidation

The a2a_mcp reference sample (from a2aproject/a2a-samples) uses a single MCP server for both agent registry and data tools. FinSight originally had 6 separate MCP servers. We consolidated into one:

**Before:** yfinance (8010), SEC EDGAR (8020), Financial News (8025), Python Runner (8040), Reddit (8030), Agent Registry (10200)

**After:** Single `finsight_server.py` (8010) with all tools + agent card registry

This matches the a2a_mcp pattern and simplifies deployment (one server to start instead of six).

## Declarative Agent Cards

Agent cards moved from Python code (`AgentCard(...)`) to JSON files (`agent_cards/*.json`). Benefits:
- Adding/modifying agents requires no code changes — just a JSON file
- Agent card registry can be queried dynamically via MCP resources
- The same server code can be reused with different cards (as in a2a_mcp)

## MCP for Tool Access

MCP tools are dynamically discovered. On connect, the client calls `list_tools()` on the server, then routes any `call_tool_by_name()` call to the correct tool. Agents don't need to know which server hosts a tool.

## Ollama vs Groq

The initial implementation used Groq (free tier via API) for all LLM calls. After hitting daily rate limits (100K tokens/day), we switched to local inference via Ollama.

| Factor | Groq | Ollama (local) |
|---|---|---|
| Speed | ~200 t/s | ~5-10 t/s |
| Latency | ~1-2s per call | ~10-30s per call |
| Rate limits | 6K TPM / 100K TPD | None |
| Cost | Free tier, need upgrade | Free, just hardware |
| Setup | API key only | Download 2GB model |

### Sentiment agent performance

Pre-collect MCP data in parallel using `asyncio.gather`, then pass it as context to the CrewAI agents. This eliminated tool hallucinations and made the workflow faster.

## LlamaIndex Router Failed

The `RouterQueryEngine` with `LLMMultiSelector` kept failing because the Groq model returned malformed JSON for the routing decision. Fix: fallback queries the SEC filings index directly.

## What Didn't Work

1. **Google ADK `LiteLlm` wrapper**: `PydanticSerializationError`. Switched to plain model string with `openai/` prefix.
2. **Streaming with raw HTTP**: Switched to direct HTTP POST for the ADK Web agent's tool functions.
3. **CrewAI hosted_vllm provider**: Switched to the `groq/` prefix with litellm.
4. **new_agent_text_message**: Doesn't exist in the installed a2a-sdk v1.0.2. Use `new_text_message` instead.
5. **Per-call A2ADiscoverer**: Creating a new discoverer on every tool call caused race conditions when the ADK agent ran tools in parallel. Fixed with `asyncio.Lock` singleton.
