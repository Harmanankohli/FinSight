# Design Decisions

## Why Four Different Agent Frameworks?

| Agent | Framework | Why |
|---|---|---|
| Orchestrator | **Google ADK** | A2A protocol built-in, agent card generation, session management |
| RAG | **LlamaIndex** | Best document indexing/retrieval — hybrid search, multi-index routing |
| Quant | **LangGraph** | Conditional state machine maps naturally to graph-based architecture |
| Sentiment | **CrewAI** | Multi-agent role-playing (analysis + synthesis) is what CrewAI was designed for |

## A2A Communication

We use the official Google A2A SDK (`a2a-sdk>=1.0.0`) for inter-agent communication.

### Key lessons

1. **messageId is required** on every A2A Message
2. **agentInterface must match**: `ClientFactory` requires `supported_interfaces` with `protocol_binding="JSONRPC"`
3. **Timeout propagation**: Both `ClientConfig` + `httpx.AsyncClient` AND `ClientCallContext` must be configured
4. **Response format**: Sub-agents return `data` (structured) not `text` — use `MessageToDict` for extraction

## Orchestrator Evolution

### v1 — REST Gateway + Planner
Raw Starlette REST API (`gateway.py`) with regex-based `planner.py`, custom `A2AClient`, and `report_generator.py`. Three overlapping orchestrator files (`gateway.py`, `orchestrator.py`, `agents/finsight_agent/agent.py`).

**Problems**: Duplicated logic, no A2A-native protocol handling, manual HTTP endpoints.

### v2 — Dynamic Per-skill ADK Tools
ADK `LlmAgent` with one tool per agent skill, generated dynamically at module import. MCP + seed URL discovery.

**Problem**: Module-level `asyncio.run(create_agent())` fails with "asyncio.run() cannot be called from a running event loop" when ADK Web UI imports the module (the UI already has a running event loop).

### v3 — Thread-based Async Initialization
Wrapped `asyncio.run(create_agent())` in a thread to bypass the running event loop restriction.

**Problem**: httpx `RuntimeError: Event loop is closed` on subsequent requests. The httpx.AsyncClient was created in the thread's short-lived event loop, then used from the main loop. Connection cleanup tried to close connections using the already-dead thread loop.

### v4 — Sync Discovery + Lazy Async A2A (current)
Sync `httpx.Client` for startup discovery (no event loop needed). A2A clients created lazily via `create_client()` on first tool call, in the correct async event loop. Cached per agent for subsequent calls.

## Problems Encountered During Streamlining

### 1. `asyncio.run()` and Running Event Loops

**Problem**: `root_agent = asyncio.run(create_agent())` at module level fails when ADK Web UI imports the module. The UI already has a running asyncio event loop, and `asyncio.run()` cannot be called from within one.

**Attempted solutions:**
- Thread-based init → httpx event loop conflicts (see below)
- `nest_asyncio` → adds dependency, doesn't fix the root cause

**Final solution**: Sync `httpx.Client` for discovery at module level (no event loop needed). A2A messaging uses async but is deferred to first tool call via `_get_a2a_client()` with lazy `create_client()`.

### 2. httpx Event Loop Conflicts from Threaded Init

**Problem**: When `create_agent()` ran in a thread (to bypass the running event loop restriction), the `httpx.AsyncClient` was created in the thread's event loop. On subsequent tool calls from the main loop, httpx tried to close connections from the thread's loop, which was already destroyed → `RuntimeError: Event loop is closed`.

**Root cause**: httpx's connection pool stores references to the event loop that created it. Using the client from a different loop causes cleanup failures.

**Solution**: `httpx.AsyncClient` is never created in a thread or at module level. It's created lazily inside `_get_a2a_client()` via `create_client()`, which runs in the main async event loop.

### 3. httpx.Timeout Constructor Ambiguity

**Problem**: `httpx.Timeout(read=300.0, connect=10.0)` — this fails because `httpx.Timeout` requires either a single value (applied to all timeout types) or all four parameters explicitly (`connect`, `read`, `write`, `pool`). Passing only two caused `httpx.Timeout must either include a default, or set all four parameters explicitly`.

**Fix**: Use `httpx.Timeout(300.0)` (single value applies to all timeout types).

### 4. Sub-agent Responses in `data` Format Not Extracted

**Problem**: The orchestrator was calling sub-agents but receiving empty responses. Sub-agents use `GenericAgentExecutor` which creates artifacts with `Part(data=Value(struct_value=s))` for structured responses (`"response_type": "data"`). Our `_extract_text()` only checked `part.text`, missing `part.data`.

**Fix**: `_extract_text()` now checks `part.data` first, converting via `MessageToDict(part.data)` for protobuf Struct serialization, then falls back to `part.text`.

### 5. MCP Resource URI Type Mismatch

**Problem**: `'AnyUrl' object has no attribute 'startswith'` — the `mcp` library returns resource URIs as Pydantic `AnyUrl` objects, not plain strings. Calling `.startswith()` directly failed.

**Fix**: Convert to string first with `str(uri)` before calling string methods.

### 6. ClientConfig Has No `timeout` Parameter

**Problem**: `ClientConfig(timeout=300, streaming=False)` failed with `unexpected keyword argument 'timeout'`. The A2A SDK's `ClientConfig` is a dataclass with fields: `streaming`, `polling`, `httpx_client`, `grpc_channel_factory`, `supported_protocol_bindings`, `use_client_preference`, `accepted_output_modes`, `push_notification_config`. No `timeout` field.

**Fix**: Pass a pre-configured `httpx.AsyncClient(timeout=httpx.Timeout(300))` via `ClientConfig(httpx_client=http)`. Additionally pass `ClientCallContext(timeout=300)` for each `send_message()` call.

### 7. LLM Tool Name Hallucination

**Problem**: Local models (llama3.2:3b, deepseek-r1:7b) generated wrong tool names when presented with multiple tools. Instead of calling `financial_rag_agent`, they called `investment_orchestrator` (the agent's own name) or generic `call_function`.

**Root cause**: Small local models don't reliably adhere to OpenAI function-calling definitions, especially with multiple tools.

**Solutions tested:**
- Different model providers (`ollama/` vs `openai/`) — `openai/` prefix worked better
- Different models — only `qwen2.5:7b` was reliable
- Single tool approach — user rejected, wanted per-agent tools
- Instruction prompt clarity — helped but wasn't sufficient alone

**Final solution**: `qwen2.5:7b` via `openai/` prefix (OpenAI-compatible endpoint on Ollama). The `ollama/` prefix caused formatting issues with LiteLLM's Ollama provider. Direct API testing confirmed `qwen2.5:7b` correctly calls multiple tools with the OpenAI format.

### 8. Agent Name Validation Error

**Problem**: `LlmAgent(name="FinSight Orchestrator")` raised `Value error: Agent name must be a valid identifier` — spaces not allowed. Agent names must start with a letter/underscore and contain only letters, digits, and underscores.

**Fix**: Changed to `name="orchestrator"`.

### 9. Slow-Starting Sub-agents Not Discovered

**Problem**: When the ADK Web UI loaded the orchestrator module, some sub-agents (especially the RAG agent, which imports llama-index) weren't fully booted yet. Discovery found 0-2 agents instead of 3.

**Fix**: `discover_sync()` now retries each failed URL up to 3 times with a 5-second delay between attempts.

### 10. MCP Registry Discovery Not Ported to Sync Path

**Problem**: The original async `A2ADiscoverer` had MCP registry discovery (`_discover_via_mcp_registry`), but the streamlined `SubAgentClient` only supported seed URLs. The sync discovery path didn't include MCP resource-based agent card discovery.

**Status**: Not yet ported. Currently only seed URL discovery works. MCP registry discovery is pending future work.

## Sync Discovery (Why Not Async)

The ADK Web UI imports the agent module synchronously. Using `asyncio.run()` at module level fails with "cannot be called from a running event loop." Using threads created httpx event loop conflicts.

**Solution**: Sync `httpx.Client` for discovery (no event loop needed). Async `create_client()` for A2A calls (lazy, correct event loop).

## Timeout Strategy

The default `create_client()` creates an httpx client with default timeouts (~5s). Sub-agent analyses (yfinance data download, ChromaDB queries, CrewAI execution) routinely exceed this.

**Fix**: Pass a pre-configured `httpx.AsyncClient(timeout=httpx.Timeout(300))` via `ClientConfig(httpx_client=http)`, plus `ClientCallContext(timeout=300)` for each send call.

## Model Selection

| Model | Verdict | Reason |
|---|---|---|
| `qwen2.5:7b` | ✅ | Reliable tool calling, good instruction following, ~4.7GB |
| `llama3.2` (3B) | ❌ | Tool calling unreliable via both `ollama/` and `openai/` providers |
| `deepseek-r1` (7B) | ❌ | Does not support tool/function calling |

**Key**: The `openai/` prefix (LiteLLM OpenAI-compatible provider) sends tool definitions in the correct format to Ollama's API. The `ollama/` prefix (LiteLLM Ollama native provider) had formatting issues. Direct API test with `curl` confirmed this — the OpenAI format works, the Ollama native format doesn't.

## RAG Agent Auto-ingest

The RAG agent fetches SEC filings via MCP on first query (`_ensure_ingested`). This was fragile with `json.loads()` on potentially empty MCP responses. Fixed with proper empty-check and `try/except json.JSONDecodeError`.
