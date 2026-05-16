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

### A2A API Reference vs Installed SDK

The A2A SDK has evolved significantly. The official A2A samples (Google, GitHub) reference APIs that may not match the installed version:

| Component | GitHub Samples | Installed SDK |
|---|---|---|
| Client | `A2AClient(httpx, card, url)` | `ClientFactory(config).create(card)` |
| Card resolution | `A2ACardResolver(client, url)` | `A2ACardResolver(client, url)` (same) |
| Well-known path | `/.well-known/agent.json` | `/.well-known/agent-card.json` |
| Server app | `A2AStarletteApplication` | Starlette + `create_agent_card_routes` + `create_jsonrpc_routes` |
| AgentCard type | Pydantic model | Protobuf message |

This project targets the **installed SDK's** API (`a2a-sdk` from PyPI), not the GitHub sample code.

### Key lessons

1. **messageId is required** on every A2A Message
2. **agentInterface must match**: `ClientFactory` requires `supported_interfaces` with `protocol_binding="JSONRPC"`
3. **Timeout propagation**: Both `ClientConfig` + `httpx.AsyncClient` AND `ClientCallContext` must be configured
4. **Response format**: Sub-agents return `data` (structured) not `text` — extract via `get_data_parts()` from `a2a.helpers`
5. **Streaming events**: The streaming `BaseClient.send_message()` yields `StreamResponse` events. Intermediate `SUBMITTED`/`WORKING` status updates must be skipped — only process `artifact_update` events and terminal `COMPLETED`/`FAILED` states
6. **Data > text**: Sub-agents using `GenericAgentExecutor` emit `Part(data=Value(struct_value=s))` for structured responses. `get_stream_response_text()` misses these — use `get_data_parts()` on artifact parts directly

### How our A2A pattern compares to reference projects

| Aspect | Google Samples | bhancockio/agent2agent | theailanguage/a2a_samples | FinSight |
|---|---|---|---|---|
| Client creation | `ClientFactory` | `A2AClient(httpx, card, url)` | Custom `A2AClient` (httpx POST) | `ClientFactory` (SDK current) |
| Streaming | Streaming (events) | Non-streaming (single response) | Non-streaming (single task) | Streaming with correct event routing |
| Host tools | `send_message(name, msg)` | `send_message(name, task)` | `delegate_task(name, msg)` | `send_message(name, task)` |
| list_agents tool | Some have it (unused) | No | Yes | Removed — agents in prompt |
| Sub-agent response | Parts + artifacts | Parts from JSON | Task history text | Data parts then text then fallback |
| Discovery background | `loop.create_task()` | `asyncio.run()` at module | Not async | Both paths (loop/run) |

## Orchestrator Evolution

### v1 — REST Gateway + Planner
Raw Starlette REST API (`gateway.py`) with regex-based `planner.py`, custom `A2AClient`, and `report_generator.py`. Three overlapping orchestrator files.

**Problems**: Duplicated logic, no A2A-native protocol handling, manual HTTP endpoints.

### v2 — Dynamic Per-skill ADK Tools
ADK `LlmAgent` with one tool per agent skill, generated dynamically at module import. MCP + seed URL discovery.

**Problem**: Module-level `asyncio.run(create_agent())` fails when ADK Web UI imports the module.

### v3 — Thread-based Async Initialization
Wrapped `asyncio.run()` in a thread to bypass the running event loop restriction.

**Problem**: httpx `RuntimeError: Event loop is closed` — connections created in thread's loop, used from main loop.

### v4 — Sync Discovery + Lazy Async A2A
Sync `httpx.Client` for startup discovery (no event loop needed). A2A clients lazily via `create_client()` on first tool call.

**Problem**: Sync HTTP for discovery was non-standard, didn't use `A2ACardResolver`. Each sub-agent was one ADK tool — LLM couldn't call them in parallel anyway.

### v5 — A2A Sample Pattern (current)
Background async `A2ACardResolver` discovery (standard well-known endpoint). `ClientFactory` for transport. Single `send_message` tool (LLM routes by name). Correct streaming event handling. No pre-fetch.

**Key insight**: Match the pattern of ALL reference projects — one `send_message` tool, LLM routes sequentially, no `list_remote_agents` (agents are in prompt already).

## Problems Encountered

### 1. `asyncio.run()` and Running Event Loops

**Problem**: Module-level `asyncio.run()` fails when ADK Web UI imports the module (already has a running event loop).

**Final solution**: Check for an existing loop first. If one is running, use `loop.create_task()` for background discovery. Otherwise, use `asyncio.run()`.

### 2. httpx Event Loop Conflicts from Threaded Init

See v3 above. `httpx.AsyncClient` is never created in a thread or at module level.

### 3. httpx.Timeout Constructor Ambiguity

`httpx.Timeout(read=300.0, connect=10.0)` fails — must pass all four or a single value. Use `httpx.Timeout(300.0)`.

### 4. Sub-agent Responses in `data` Format Not Extracted

**Problem**: `get_stream_response_text()` only returns text. Sub-agents return structured `data` parts. Our code got empty results.

**Fix**: Check `get_data_parts(artifact.parts)` first, then fall back to `get_artifact_text(artifact)`.

### 5. Streaming Event Handling — Early Return on WORKING

**Problem**: `get_stream_response_text()` extracts text from ANY event including `WORKING` status messages. The LLM got `"Running Financial RAG Agent..."` as the "result" and called `send_message` again in an infinite loop.

**Fix**: Route events by type: skip `SUBMITTED`/`WORKING` status updates, only process `artifact_update` (data or text), terminal `status_update`, and terminal `task` events.

### 6. Non-terminal Task Events Returned as Result

**Problem**: The first streaming event is `task { state: SUBMITTED }`. Without checking terminal state, this was returned as `{"id": "xxx", "state": 1}`, confusing the LLM.

**Fix**: Check `task.status.state not in _TERMINAL_STATES` before processing a task event.

### 7. Local LLMs Don't Support Parallel Function Calling

**Problem**: The LLM instruction says "call ALL agents simultaneously" but no local or low-end model supports parallel tool calling.

**Final solution**: Use a single `send_message` tool. The LLM calls agents sequentially, which matches the pattern used by ALL reference A2A projects.

### 8. MCP Resource URI Type Mismatch

`'AnyUrl' object has no attribute 'startswith'` — convert `AnyUrl` to string with `str(uri)`.

### 9. ClientConfig Has No `timeout` Parameter

Pass pre-configured `httpx.AsyncClient(timeout=httpx.Timeout(300))` via `ClientConfig(httpx_client=http)`.

### 10. LLM Tool Name Hallucination

Small local models generated wrong tool names. Fixed by: single `send_message` tool (no name to get wrong).

### 11. Agent Name Validation Error

`LlmAgent(name="FinSight Orchestrator")` — spaces not allowed. Use `name="orchestrator"`.

### 12. Slow-Starting Sub-agents Not Discovered

`discover()` retries each URL 3 times with 5-second delay.

### 13. MCP Registry Discovery Not Ported

MCP resource-based agent card discovery is pending future work.

### 14. Windows ConnectionResetError Noise

`ConnectionResetError: [WinError 10054]` on Windows after successful A2A calls. Caused by ProactorEventLoop shutting down sockets already closed by the remote side.

**Fix**: `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())` on Windows.

### 15. AgentCard Protobuf — No `url` Field

**Problem**: The SDK's `AgentCard` is a protobuf message with no `url` field. Construction like `AgentCard(url="http://...")` raises `ValueError`.

**Fix**: Set `supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=...)]` instead.

## Background Async Discovery

The ADK Web UI imports the agent module synchronously. Using `asyncio.run()` at module level fails if a loop is already running. Using threads caused httpx event loop conflicts.

**Solution**: Detect running loop at import time:
- Loop running → `loop.create_task(discover_background())`
- No loop → `asyncio.run(discover_background())`

## Timeout Strategy

The default `create_client()` creates an httpx client with default timeouts (~5s). Sub-agent analyses routinely exceed this.

**Fix**: Pass a pre-configured `httpx.AsyncClient(timeout=httpx.Timeout(300))` via `ClientConfig(httpx_client=http)`.

## Model Selection (Ollama Era)

| Model | Verdict | Reason |
|---|---|---|
| `qwen2.5:7b` | ✅ | Reliable tool calling, good instruction following, ~4.7GB |
| `llama3.2` (3B) | ❌ | Tool calling unreliable via both `ollama/` and `openai/` providers |
| `deepseek-r1` (7B) | ❌ | Does not support tool/function calling |

**Key**: The `openai/` prefix (LiteLLM OpenAI-compatible provider) sends tool definitions in the correct format.

## Migration from Ollama to LM Studio

### Problem: Ollama was too slow

Ollama's inference speed for `qwen2.5:7b` was 20-40 seconds per LLM call. With the orchestrator calling all three sub-agents sequentially, a single query took 2-3 minutes.

### Solution: LM Studio

LM Studio provides faster inference, OpenAI-compatible API, simpler setup.

### Changes made

| Area | Before (Ollama) | After (LM Studio) |
|---|---|---|
| Base URL | `http://localhost:11434/v1` | `http://localhost:1234/v1` |
| Model name | `qwen2.5:7b` | `gpt-oss-20b` |
| Agent 1 (ADK) | `openai/qwen2.5:7b` | `openai/gpt-oss-20b` |
| Agent 2 (LlamaIndex) | `llama-index-llms-ollama` | `llama-index-llms-openai-like` |
| Agent 3 (LangGraph) | `langchain-ollama` | `langchain-openai` |
| Agent 4 (CrewAI) | `CrewLLM(model="ollama/...")` | `CrewLLM(model="gpt-oss-20b")` |

## MCP Server Design

The unified finsight MCP server (`finsight_server.py`) hosts agent registry + data tools on port 8010.

### Lazy Agent Registry

**Problem**: `sentence-transformers` downloads the embedding model (~80MB) at import time. When ADK Web UI or MCP host imports the module, model download blocks startup and may fail in restricted environments.

**Solution**: Defer model loading to first tool call via `_ensure_registry()` with `asyncio.Lock` double-checked locking. Model is loaded once in a thread executor, never at module level.

### Windows Compatibility

**Problem**: `import resource` (Unix RLIMIT) raises `ModuleNotFoundError` on Windows.

**Solution**: Guard with `if sys.platform != "win32": import resource`. Sandbox `preexec_fn` is `None` on Windows (RLIMIT is Unix-only).

### Thread-Safe SSE App Singleton

**Problem**: FastMCP's `sse_app()` creates a new Starlette app instance each call. Under concurrent reload or multi-worker setups, this duplicates middleware, routes, and lifecycle hooks, causing `RuntimeError: Lifespan context has already been started`.

**Solution**: `get_app()` with a `threading.Lock` double-checked singleton pattern.

### Inline Imports for Localised Scope

**Problem**: Top-level `import re` creates a module-wide reference. In sandbox contexts or when the module is reloaded, shadowed or patched `re` can break internal normalisation logic.

**Solution**: `import re as _re` inside `_resolve_company_keywords` and `_normalise_for_match` — guarantees a fresh, unpatched reference.

### SEC EDGAR Caching

**Problem**: Every `get_company_filings` call re-fetched the company ticker → CIK mapping (~4MB JSON from SEC.gov), adding latency and hitting SEC rate limits.

**Solution**: `_EdgarClient._get_ticker_map()` with `asyncio.Lock` lazy loading. CIK results cached in `_cik_cache`, ticker→title map cached in `_title_map`. Subsequent calls are dict lookups.

### Sandbox Hardening

**Problem**: The Python sandbox allowed potentially dangerous imports (`builtins`, `gc`, `threading`, `multiprocessing`, etc.) that could be used to escape the subprocess.

**Solution**: Expanded `_RESTRICTED_IMPORTS` and `_RESTRICTED_ATTRS` blocklists. Subprocess runs with `-I` (isolated) and `-S` (no site) flags. RLIMIT applied on Unix.

## Model Change: gpt-oss-20b → qwen

The LLM used by all agents was switched from **`gpt-oss-20b`** to a **qwen** model:

| Model | Speed | Notes |
|---|---|---|
| `gpt-oss-20b` (previous) | ~40-60s per call | Large, slower inference |
| `qwen3-30b-a3b-2507` (current) | ~5-10s per call | Much faster, sufficient quality |

**Key**: The qwen model reduced per-call latency by ~5-10x while maintaining adequate output quality for all agent tasks (routing, summarisation, analysis). This was the single biggest performance improvement in the pipeline.

## RAG Agent Auto-ingest

The RAG agent fetches SEC filings via MCP on first query (`_ensure_ingested`). Was fragile with `json.loads()` on potentially empty MCP responses. Fixed with proper empty-check and `try/except json.JSONDecodeError`.
