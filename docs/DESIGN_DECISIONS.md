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

**Initial solution**: Use a single `send_message` tool. The LLM called agents sequentially.

**Resolution with qwen**: The `qwen3-30b-a3b-2507` model supports parallel function calling, calling `send_message` for multiple agents simultaneously. The single `send_message` tool pattern is retained — agents marked as requiring sequential execution will be serialized by the LLM.

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

### Docker-compose Alignment Fix

**Problem**: `docker-compose.yml` referenced 4 separate MCP services (`mcp-yfinance`, `mcp-sec-edgar`, `mcp-reddit`, `mcp-python-runner`) that each ran different server files (`yfinance_server.py`, etc.). These files didn't exist — only the unified `finsight_server.py` existed in the repository.

**Solution**: Replaced the 4 broken services with a single `finsight-mcp` service that runs the existing `finsight_server.py`. Updated agent environment variables to point to the unified server (`MCP_SERVER_URL=http://finsight-mcp:8010/sse`). The actual codebase was already using this pattern — docker-compose was simply out of sync.

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

### RAG Content Ingestion Fix

**Problem**: RAG agent only stored SEC filing metadata (form type, description, URL) in ChromaDB, not actual filing content. Queries returned "cannot be performed based on provided information" because the index had no meaningful text.

**Solution**: 
1. Added `get_filing_content(edgar_url, ix_url)` MCP tool to fetch and extract text from raw EDGAR documents
2. Updated `get_company_filings` to return both raw document URL (`edgar_url`) and IXBRL viewer URL (`ix_url`)
3. RAG agent now calls `get_filing_content()` for each filing and stores extracted text (up to 20K chars) into ChromaDB
4. Enhanced `get_filing_content` to handle multiple content types (HTML, XML, JSON), skip XBRL viewer pages, and fallback to IX URL if raw fails

### Quant DCF Null Fix

**Problem**: DCF valuation always returned `null` because `_get_fcf_from_financials()` looked for "Free Cash Flow" in the `income_statement` dict, but FCF belongs in the `cash_flow` statement.

**Solution**: Changed `dcf_valuation_node()` to use `data.get("cash_flow", {})` instead of `data.get("income_statement", {})`.

## Ticker Extraction Decoupled from SEC Validation

### Problem

`extract_ticker()` in `shared/ticker_utils.py` fetched the full SEC company_tickers.json (~4MB) on every call to validate candidates. This meant:
1. Every agent query triggered a network call to SEC.gov just to extract a ticker
2. `RAGAgent.stream()` had a broken `await self._connect()` call (method didn't exist)
3. Validation logic was duplicated inline across three agent executors with subtle differences
4. No graceful fallback when MCP was unavailable

### Solution

**Step 1 — Pure regex extraction**: `extract_ticker()` now returns the first regex match immediately with no network calls. Priority cascade: parentheses > trigger words > $ prefix > 3-5 letter words > 2 letter words.

**Step 2 — MCP-based validation**: Each agent has a `_validate_ticker()` method that:
1. Connects MCP if not already connected
2. Calls the MCP `validate_ticker` tool (which talks to SEC EDGAR)
3. Returns `(is_valid, ticker, company_or_error)` as a uniform `tuple[bool, str, str]`

**Step 3 — First-match heuristic**: Pattern 4 (`\b([A-Z]{3,5})\b`) returns `matches[0]` (first match) rather than `matches[-1]` (last match). Rationale:

- The orchestrator LLM generates task text like *"Analyze WMT (Walmart) SEC filings for..."* — the ticker appears **first**, stop words like "SEC", "EPS", "NYSE", "INC" appear **after** it
- `matches[-1]` picked up trailing stop words: "SEC" instead of "WMT" in the task above
- `matches[0]` prefers the ticker that was mentioned first
- If the first match is wrong, `_validate_ticker` rejects it → `resolved` fallback → company name resolution catches the real ticker

**Step 4 — Validation fallback to resolution**: When `extract_ticker` returns a candidate that `_validate_ticker` rejects (e.g. regex picks up "SEC" from task context), the agent retries with `_resolve_ticker` (company name resolution) before returning an error. This creates a three-layer defense:

1. **Regex first** (instant) — catches explicit tickers: "(AAPL)", "$V", "for MA"
2. **Company name resolution** (SEC reverse index + Yahoo fallback) — catches natural language: "Mastercard" → "MA"
3. **Validation** (SEC EDGAR) — confirms ticker exists, used as gate for all of the above

**Step 5 — Ticker format gate**: `is_valid_ticker_format()` rejects anything that doesn't match `^[A-Z]{1,5}(\.[A-Z]{1,2})?$` — prevents mutual fund identifiers ("0P0000SECP.F") and other non-equity symbols from reaching validation.

**Key properties**:
- ✅ Extraction never fails — pure regex, no network calls
- ✅ Validation is optional — MCP validation has fallback to raw regex guess
- ✅ No SEC API from agent side — only MCP server talks to SEC
- ✅ MCP server caches — SEC map loaded once, cached per server lifetime
- ✅ Backward compatible — all existing patterns still work

## Financial Filings Tool (get_financial_filings)

### Problem

`get_company_filings()` returned all recent filings in order, but for large financial companies the "recent" batch was dominated by 8-Ks (current reports filed nearly daily). A request for `limit=10` might return 0-1 actual 10-K or 10-Q statements, leaving RAG agents with no financial data to analyze.

### Solution

Added `get_financial_filings()` that fetches 10-K and 10-Q filings separately with independent limits:

```
annual_limit=5    → up to 5 years of 10-Ks
quarterly_limit=8 → up to 2 years of 10-Qs
```

If the initial "recent" batch doesn't contain enough 10-Ks, it paginates to older filings pages. The response separates annual from quarterly so downstream agents can distinguish yearly trends from quarterly updates.

## News System: Concurrent RSS + Yahoo Finance Fallback

### Problem

The original RSS pipeline had three issues:
1. Feeds were fetched **sequentially** — if MarketWatch timed out, CNBC and Yahoo waited
2. No failure diagnostics — a blank response looked identical to "no news for this ticker"
3. No fallback — if all three RSS feeds returned zero matches, the agent got empty news with no explanation

### Solution

**Concurrent fetching**: All three RSS feeds are fetched simultaneously via `asyncio.gather()`. A slow/unreachable feed doesn't block the others.

**Structured return values**: `_fetch_rss()` returns `{"entries": [...], "status": "ok" | "http_xxx" | "parse_error" | "error", "error": "..."}`. Each source gets an entry in `feed_status` so agents can see which feeds worked.

**Yahoo Finance news API fallback**: When all RSS feeds are unreachable **or** return zero keyword-matched articles, `_fetch_yf_news()` queries Yahoo Finance's structured `v1/finance/search` API. Unlike RSS, results are pre-filtered to the ticker — no keyword matching needed.

**Better diagnostics**: `source_used` tells the agent whether results came from RSS or the Yahoo fallback. `feed_status` shows per-source HTTP status codes. The response distinguishes:
- `rss_unreachable` — feeds returned errors
- `rss_no_match` — feeds returned articles but none matched the ticker

### MCP Response Parsing Inconsistency

**Problem**: Each agent parsed MCP tool responses differently (checking for `.content`, `.text`, dict vs list, etc.), leading to fragile error handling.

**Solution**: Added `parse_mcp_result(result)` utility in `shared/mcp_client.py` that handles various MCP response formats consistently — returns parsed dict/list/string or `{"error": "..."}` on failure.

## DCF Skipped from High Volatility Routing

### Problem

`dcf_valuation` returned `null` for tickers with annual volatility above 35% (e.g. Oracle at 41%). The graph's `_route_on_volatility` function routed these to `stress_test` and DCF was never called. The `dcf_error` field was `null` too — making it impossible to distinguish "DCF failed" from "DCF was never executed".

### Solution

Three changes across the graph pipeline:

1. **Set `dcf_error` in `compute_metrics_node`**: When `annual_vol > 0.35` is detected, the metrics node now includes a descriptive `dcf_error` message (e.g. "DCF skipped: annual volatility (41.0%) exceeds 35% threshold – routed to stress test instead"). This is set *before* the routing decision, so it's available in state regardless of which path is taken.

2. **Surface `dcf_error` in `graph.run()` output**: The result dict now includes the `dcf_error` field so callers can see why DCF is null.

3. **Include `dcf_error` in reasoning**: `format_output_node` appends the error to the reasoning string when DCF is null with an error, so the LLM summary has full context.

### Key properties
- ✅ Callers can distinguish "DCF not computed" from "DCF failed to compute"
- ✅ The error reason appears in both structured output (`dcf_error` field) and natural language summary (reasoning text)
- ✅ No false positives — only set when volatility routing actually causes the skip

## DCF Null from Negative Free Cash Flow

### Problem

`_get_fcf_from_financials()` returned the most recent period's FCF regardless of sign. For financial companies like JPM, the latest year's FCF was negative (large capital expenditures / investment purchases), causing the `latest_fcf <= 0` guard to return `{"dcf_valuation": None}` with no explanation.

### Solution

Changed `_get_fcf_from_financials()` to iterate through periods and return the **first positive FCF** instead of the first period's raw value. Added comprehensive failure logging:

1. **No MCP client** — logs "no MCP client" 
2. **Empty response** — logs "MCP returned empty response"
3. **No cash flow data** — logs "no cash flow data available"
4. **No positive FCF** — logs actual FCF values from both `Free Cash Flow` field and `Operating Cash Flow + Capital Expenditure` calculation
5. **Missing shares outstanding** — logs the invalid value
6. **Missing current price** — logs the invalid value

All failures return a `dcf_error` string alongside `dcf_valuation: null`, which is surfaced in the agent response.

## Ticker False Positives from Financial Acronyms

### Problem

Pattern 4 of `extract_ticker()` (`\b([A-Z]{3,5})\b`) matched any uppercase word of 3-5 characters, including common financial acronyms like "SEC", "EPS", "CEO", "NYSE", "NASDAQ". When a user asked "Analyze General Electric SEC filings", the first match was "SEC" instead of "General Electric".

Validation rejected "SEC", but the fallback `_resolve_ticker()` passed the full noisy query "Analyze General Electric SEC filings for recent financial performance" to MCP's `resolve_company_ticker`. The SEC reverse index could not reliably match against all those noise words, and Yahoo Finance occasionally returned irrelevant tickers.

### Solution

**Step 1 — Financial stop-word blocklist**: Added `_FINANCIAL_STOP_WORDS` in `shared/ticker_utils.py` — a curated set of 30+ financial acronyms that are never valid stock tickers. Applied to both pattern 4 (3-5 letter) and pattern 5 (2 letter) regex results.

```python
_FINANCIAL_STOP_WORDS = frozenset({
    "SEC", "EPS", "CEO", "CFO", "NYSE", "NASDAQ",
    "INC", "LLC", "LTD", "CORP", "GAAP", "EBIT", "EBITDA",
    ...
})
```

**Step 2 — Query noise cleanup**: Added `clean_query_for_resolution()` that strips:
- Common financial analysis words ("analyze", "filings", "financial", "performance", "sec", "edgar")
- Generic English stop words ("the", "a", "for", "about", "this", "that")
- Words from `_FINANCIAL_STOP_WORDS` (uppercase variants like "SEC", "INC")

**Step 3 — Exclude ticker from resolution**: `_resolve_ticker(query, exclude_ticker="SEC")` strips the regex-extracted false positive from the query before calling MCP. So "Analyze General Electric SEC filings" → "General Electric" after cleanup.

**Key properties**:
- ✅ No false positives from financial jargon
- ✅ Company name resolution receives clean input
- ✅ Failed ticker is excluded from the resolution query
- ✅ All three agents (RAG, Quant, Sentiment) apply the same cleanup
- ✅ Backward compatible — all existing patterns still work

## Portfolio Holdings Extraction for Correlation Analysis

### Problem

The Quant agent's `correlation_matrix` was always `{}` even when users explicitly mentioned portfolio holdings. The `correlation_node` in `nodes.py` requires `portfolio_holdings` (a list of ticker symbols) to compute correlations, but the chain never populated it:

```
stream() → analyze(ticker) → graph.run(ticker, portfolio_holdings=None)
```

The `QuantAgent.stream()` method extracted the target ticker from the query but had no logic to extract portfolio holdings. Even though `graph.run()` accepted a `portfolio_holdings` parameter, it was always passed as `None`.

### Solution

**Step 1 — `extract_holdings()` in `shared/ticker_utils.py`**: Four regex patterns covering natural language phrasing:

```python
_HOLDINGS_PATTERNS = [
    # "My portfolio holds AAPL, MSFT, GOOGL"
    # "My portfolio: TSLA, AMZN, META"
    re.compile(r"(?:portfolio|holdings?)\s*(?::|holds?|contains?|includes?|consists\s+of)\s*..."),
    # "I own MSFT and GOOGL"
    # "my current portfolio includes AAPL, TSLA"
    re.compile(r"(?:I\s+(?:own|hold|have|am\s+invested\s+in)|my\s+...portfolio...)\s+..."),
    # "My current holdings are JPM, BAC, WFC"
    re.compile(r"(?:my\s+...portfolio...)\s+are\s+..."),
    # "currently own AAPL, MSFT"
    re.compile(r"(?:currently\s+)?(?:own|hold|have)\s*:?\s*..."),
]
```

Each pattern captures a comma-and-separated list of uppercase tickers. The `exclude_ticker` parameter removes the target stock from the holdings list.

**Step 2 — Pass holdings through the chain**: `stream()` calls `extract_holdings(query, exclude_ticker=ticker)`, passes to `analyze(portfolio_holdings=holdings)`, which passes to `graph.run(portfolio_holdings=holdings)`.

**Step 3 — Orchestrator LLM instruction updated**: Added step 4 to the orchestrator system prompt telling the LLM to include portfolio holdings in the task text for the Quant Analysis Agent. Without this, the LLM would drop holdings from the generated task.

**Step 4 — Helpful notes instead of empty `{}`**: When no holdings are provided, `correlation_node` returns `{"note": "No portfolio holdings provided..."}`. When price data is insufficient, returns `{"note": "Insufficient overlapping price data..."}`. On exception, returns `{"error": "..."}`.

### Key properties
- ✅ Holdings extraction is pure regex — no network calls, instant execution
- ✅ Target ticker excluded from holdings to avoid self-correlation
- ✅ Works with comma-separated, "and"-connected, and mixed formats
- ✅ Backward compatible — returns `[]` when no holdings mentioned

## Langfuse Span Noise Filtering

### Problem

With `should_export_span=lambda span: True`, Langfuse exported every single span including noisy A2A internal spans. Each A2A `send_message` call generated multiple internal spans from the `a2a-python-sdk` instrumentation scope (HTTP transport, JSON-RPC serialization, event handling). As the number of agents grew, this made Langfuse traces extremely noisy and hard to debug.

### Solution

Use Langfuse's built-in `is_default_export_span` helper which exports spans only from:
- `langfuse-sdk` scope (our manual `start_observation` calls — high-level workflow)
- `gen_ai.*` attribute spans (actual LLM calls)
- Known LLM instrumentors (`litellm`, `openinference.*`, `langsmith`, `haystack`, `agent_framework`, etc.)

This filters out `a2a-python-sdk`, `opentelemetry.instrumentation.httpx`, and other infrastructure scopes automatically.

### What's exported vs filtered

| Span Type | Instrumentation Scope | Exported? |
|---|---|---|
| `finsight-query` trace | `langfuse-sdk` | ✅ |
| `orchestrator-execute` | `langfuse-sdk` | ✅ |
| `rag-agent-stream` | `langfuse-sdk` | ✅ |
| `quant-agent-stream` | `langfuse-sdk` | ✅ |
| `sentiment-agent-stream` | `langfuse-sdk` | ✅ |
| LLM calls | `litellm`, `openinference.*` | ✅ |
| LangGraph nodes | `langfuse-sdk` (via CallbackHandler) | ✅ |
| A2A `send_message` internal | `a2a-python-sdk` | ❌ |
| A2A `DefaultRequestHandler` | `a2a-python-sdk` | ❌ |
| HTTPX transport spans | `opentelemetry.instrumentation.httpx` | ❌ |

### Tradeoff

If you need to **temporarily debug** and see all spans (including A2A internals), switch back to `should_export_span=lambda span: True`. The default filter is the recommended production setting per [Langfuse maintainer guidance](https://github.com/orgs/langfuse/discussions/8366).

## Langfuse Distributed Tracing Across Processes

### Problem

Each agent runs in a separate OS process (uvicorn on its own port). When a sub-agent calls `langfuse.start_observation()` it creates a brand new root trace — Langfuse has no way to know that the sub-agent trace belongs inside the orchestrator's trace. This resulted in 4 disconnected traces per query:

```
Trace A: orchestrator-execute   [pid: 8001]
Trace B: rag-agent-stream        [pid: 8002]   ← orphan
Trace C: quant-agent-stream      [pid: 8003]   ← orphan
Trace D: sentiment-agent-stream  [pid: 8004]   ← orphan
```

### Solution

**Text-based trace context injection via A2A payload:**

1. **Orchestrator** (`sub_agent_client.py`): Extracts `trace_id` and `parent_span_id` from the current Langfuse context via `lf.get_current_trace_id()` and `lf.get_current_observation_id()`. Serializes them as a JSON prefix: `{"_trace": {"trace_id": "...", "parent_span_id": "..."}}\n<<<TASK>>>\n{task_text}`.

2. **Sub-agents** (`executor.py`): Extract the prefix via `extract_trace_ids(query)`, rebuild the `trace_context` dict, and pass it to `langfuse.start_observation(..., trace_context=trace_ctx)`. Langfuse uses the `trace_id` to join the existing trace and `parent_span_id` to set the parent observation.

3. **LangGraph CallbackHandler**: Quant agent additionally passes `trace_context` to `CallbackHandler(trace_context=trace_ctx)` so all internal graph nodes are linked to the parent trace.

### Why not OpenTelemetry W3C TraceContext headers?

The A2A SDK controls the HTTP transport layer. Injecting custom headers would require modifying the SDK client or using httpx event hooks. The text-prefix approach is simpler, already partially implemented, and works reliably across all A2A transports (JSON-RPC, HTTP+JSON).

### Why `start_observation()` not `start_as_current_observation()`?

`start_as_current_observation()` is a context manager that manages OTel context tokens. In async generators, the context token is created in one async context but the generator yields control to a different context, causing `ValueError: Token was created in a different Context`. `start_observation()` creates the span manually without OTel context management, avoiding the conflict. The span's `.end()` is called in the `finally` block.

### Result

```
Trace A: finsight-query [ticker=NVDA]
├── orchestrator-execute
│   ├── send_message → Financial RAG Agent
│   │   └── rag-agent-stream (child of orchestrator-execute)
│   ├── send_message → Quant Analysis Agent
│   │   └── quant-agent-stream (child of orchestrator-execute)
│   │       ├── fetch_prices (LangGraph node)
│   │       ├── compute_metrics
│   │       ├── run_dcf / run_stress_test
│   │       └── llm_summary
│   └── send_message → Sentiment Intelligence Agent
│       └── sentiment-agent-stream (child of orchestrator-execute)
```

## Date Hallucination

### Problem

All LLM prompts lacked temporal context. Since the model's training data cut off before 2026, it treated 2026 filing dates as "future-dated anomalies" and instructed users to wait for "Q1 2024 earnings" — two-year-old data.

### Solution

Added `Today's date: {date.today().isoformat()}` as the first line of every LLM prompt:
- **Orchestrator system prompt** — so it includes the date when constructing sub-agent tasks
- **RAG query prompts** — so the LlamaIndex LLM knows the reference date for financial data
- **Quant summary prompt** — so the LangGraph summary LLM frames analysis in correct temporal context
- **Sentiment crew tasks** — so CrewAI agents know the current date when analyzing news and filings

## Persistent Memory Layer

### Problem

ADK's default `InMemoryMemoryService` loses all conversation history on server restart. The `DatabaseSessionService` persists sessions to SQLite but doesn't expose them for cross-session search. The `load_memory` tool returned empty results because:

1. `adk web` uses `InMemoryMemoryService` (in-memory, lost on restart)
2. `InMemorySessionService.get_session()` returns a `Session` with an empty `events` list — events are stored in the DB but never loaded back into the session object
3. Our initial `SQLiteMemoryService.add_session_to_memory(session)` iterated over `session.events`, which was always empty

### Solution

**Standard ADK pattern**: `run_async()` → `get_session()` → `add_session_to_memory(session)`

The `DatabaseSessionService.get_session()` loads events from the database when called after `run_async()` completes. We collect events during `run_async()` as a safety net, then use the standard pattern with fallback:

```python
session = await self._runner.session_service.get_session(...)
if session and session.events:
    await self._runner.memory_service.add_session_to_memory(session)
else:
    # Fallback: use events collected during run_async
    await self._runner.memory_service.add_events_to_memory(events=collected_events)
```

### Hybrid Search: BM25 + Embeddings

Instead of Mem0 (requires external OpenAI API), we implemented local hybrid search:

1. **BM25 keyword scoring** (`rank_bm25`) — exact term matching with TF-IDF weighting
2. **Semantic similarity** (`sentence-transformers/all-MiniLM-L6-v2`) — already in dependencies, runs locally
3. **RRF fusion** — combines both rankings, handles cases where one method fails

This gives Mem0-like search quality without external API dependencies or per-query latency.

### Schema Design

- `search_text` column added to `memory_entries` — pre-extracted plain text for fast BM25 scoring
- Auto-migration via `ALTER TABLE` — existing databases get the column on startup
- `TickerMemory`, `PortfolioStore`, `PerformanceTracker` store structured data separately from conversation events

### adk web vs main.py

`adk web` creates its own runner with `InMemoryMemoryService` (default). Our `main.py` uses `SQLiteMemoryService` with BM25 + embedding search. Both work with our `_add_events_to_memory` implementation — the standard ADK pattern is compatible with any `MemoryService`.

For production use with `adk web`, configure a custom memory service via `--memory-service-uri finsight://`.

### `load_memory` Signature Mismatch Fix (v1.14)

The `load_memory` tool returned empty results even after sessions were persisted. Root cause: ADK's `CallbackContext.add_events_to_memory()` calls with signature `(events=..., custom_metadata=None)`, but our `SQLiteMemoryService.add_events_to_memory()` required `(app_name=..., user_id=..., events=..., session_id=...)`.

**Fix**: Made `app_name` and `user_id` optional with defaults (`"finsight"` and `"default_user"`). When called via the callback, user_id, session_id, and app_name are extracted from `custom_metadata`.

**Dual persistence**: Events are persisted via two paths:
1. **`after_agent_callback`** — invoked by ADK after each agent turn (works for `adk web` UI)
2. **`_persist_to_memory`** — called directly in `agent_executor.py` after response processing (works for A2A requests)

This ensures memory works regardless of invocation path.
