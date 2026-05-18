# Changelog

## v1.10 — Langfuse Distributed Tracing Fix

- **Cross-process trace propagation fixed**: Sub-agent spans now correctly link to the orchestrator's root trace instead of creating orphan traces. Each agent process previously created its own root trace because `start_observation(trace_context=...)` was not properly linking spans across process boundaries.
- **`extract_trace_ids()` helper added**: New function in `shared/trace_context.py` that returns `(trace_id, parent_span_id, clean_query)` — a convenience wrapper over `extract_trace_context()` for the common case of needing explicit IDs.
- **`start_observation()` with explicit `trace_context`**: All three sub-agents (RAG, Quant, Sentiment) now use `langfuse.start_observation(..., trace_context=trace_ctx)` where `trace_ctx` is built from the injected `trace_id` and `parent_span_id` passed through the A2A message text prefix.
- **`CallbackHandler(trace_context=...)` for LangGraph**: Quant agent passes `trace_context` dict to Langfuse's LangChain `CallbackHandler` so internal graph nodes (fetch_prices, compute_metrics, dcf_valuation, llm_summary) are also linked to the parent trace.
- **`start_observation` over `start_as_current_observation`**: Used `start_observation()` (manual, no OTel context management) instead of `start_as_current_observation()` (context manager) because the latter conflicts with async generators — OTel context tokens are created in a different async context, causing `ValueError: Token was created in a different Context`.
- **Trace context injection unchanged**: `inject_trace_context()` in `sub_agent_client.py` already serialized `trace_id` + `parent_span_id` as a JSON prefix in the A2A task text. The fix was on the extraction/usage side.
- **8 trace propagation tests passing**: Added `test_extract_trace_ids_roundtrip` and `test_extract_trace_ids_no_context` to verify the new helper.

## v1.9 — Logging Overhaul & DCF Skip Messaging

- **Comprehensive logging added**: 11 new `logger.info/warning/debug` calls across `graph.py`, `nodes.py`, and `executor.py` — routing decisions, metric computation failures, DCF fallbacks, beta calculation errors, format-output summaries, and execution lifecycle
- **`dcf_error` now set on high-volatility route**: `compute_metrics_node` sets `dcf_error` when volatility > 35% so callers see why DCF was skipped (e.g. "DCF skipped: annual volatility (41.0%) exceeds 35% threshold – routed to stress test instead")
- **`dcf_error` included in graph output**: `graph.run()` now returns `dcf_error` in its result dict — previously only kept in state, never surfaced
- **`dcf_error` added to reasoning**: `format_output_node` includes `dcf_error` in the reasoning string when DCF is null with an error, so the LLM summary has context
- **`dcf_error: None` in initial state**: Added missing field to graph initial state for TypedDict consistency
- **`quant.log` routing diagnostics**: `_route_on_volatility` logs which branch was taken with ticker + volatility value
- **FCF debug logging**: `_get_fcf_from_financials` logs every FCF candidate examined per period (debug level) — no more silent "null" on cash flow parsing

## v1.8 — Documentation & Housekeeping

- **All docs updated**: README, TESTS, ARCHITECTURE, AGENTS, MCP_SERVERS, DEMO, CHANGELOG, DESIGN_DECISIONS synced with codebase
- **README diagram expanded**: MCP tool list updated to include all 13 tools (added `get_options_chain`, `get_financial_filings`, `get_filing_content`, `validate_ticker`, `resolve_company_ticker`, `get_earnings_calendar`)
- **README shared section**: Added `ticker_utils.py` to project structure
- **TESTS.md**: Corrected test count from 39→42, added `test_orchestrator_tools.py`, updated per-file test counts
- **ARCHITECTURE.md**: LLM models updated from `gpt-oss-20b` to `qwen/qwen3-30b-a3b-2507`; MCP tool diagram expanded
- **MCP_SERVERS.md**: Added `get_financial_filings`, `validate_ticker`, `resolve_company_ticker` tool documentation
- **DEMO.md**: Updated to reflect parallel calling with qwen model; corrected tool description to single `send_message`

## v1.7 — MCP Server Refactoring & News Fallback

- **`_EdgarClient` refactored**: Extracted `_build_filing_urls()` and `_fetch_submissions()` methods. `_INDEX_ONLY_FORMS` moved to module level. Added `FINANCIAL_FORM_TYPES` constant.
- **New `get_financial_filings` tool**: Fetches 10-K/10-Q filings separately with balanced annual/quarterly limits. Prevents the common failure of `get_company_filings` returning mostly 8-Ks. Separates annual from quarterly in response.
- **RSS fetching rewritten**: `_fetch_rss()` now returns a structured dict (`entries`, `status`, `error`) instead of a raw feedparser object. All three feeds fetched concurrently via `asyncio.gather`. Each source gets individual `feed_status` diagnostics.
- **Yahoo Finance news API fallback**: `_fetch_yf_news()` tries Yahoo Finance's structured news search when all RSS feeds are unreachable or return zero matching articles. Results are pre-filtered to the ticker — no keyword matching needed.
- **`get_news_sentiment` enhanced**: Added `feed_status` (per-source diagnostics), `source_used` field ("rss" / "yahoo_finance_api" / "none"), and improved error messaging. Distinguishes rss_unreachable vs rss_no_match.
- **Keyword matching improved**: Extracted `_resolve_company_keywords()` to build ticker+company-name keyword lists with bigrams and split-at-2 variants. `_keyword_matches()` uses word boundaries for single-word keywords to avoid substring false positives.

## v1.6 — DCF Robustness, Ticker False Positives & Date Awareness

- **DCF finds first positive FCF**: `_get_fcf_from_financials()` skips negative FCF periods instead of returning null. Logs detailed `dcf_error` at each failure point (no MCP client, empty cash flow, no positive FCF, missing shares/price).
- **`dcf_error` field in state**: New `QuantAnalysisState.dcf_error` captures exact DCF failure reason.
- **Financial stop-word blocklist**: `_FINANCIAL_STOP_WORDS` filters out "SEC", "EPS", "CEO", "NYSE", "NASDAQ", "INC", "GAAP" from regex ticker matches — prevents "Analyze GE SEC filings" from extracting "SEC" instead of "GE".
- **Query noise cleanup**: `clean_query_for_resolution()` strips analysis noise words before sending to MCP's `resolve_company_ticker`. Failed ticker candidates are excluded from the resolution query.
- **Date hallucination fix**: All LLM prompts now include `Today's date: {current_date}` — orchestrator, RAG queries, quant summary, and sentiment crew.
- **MCP: `follow_redirects=True`**: httpx client follows 301 redirects automatically.
- **MCP: CIK leading-zero fix**: EDGAR archive URLs use `str(int(cik))` instead of 10-digit zero-padded CIK, preventing 301 redirects.
- **MCP: IXBRL viewer pages parsed**: Removed "XBRL Viewer" skip — pages parsed with `html.parser`. `lxml-xml` fallback to `html.parser`.
- **MCP: MarketWatch RSS updated**: `feeds.content.dowjones.io/public/rss/mw_topstories`.

## v1.5 — Company Name to Ticker Resolution

- **New `resolve_company_ticker` MCP tool**: Resolves natural language company names ("Mastercard", "Apple") to ticker symbols via SEC reverse index (instant, local cache) with Yahoo Finance search API fallback
- **`_resolve_ticker()` on all agents**: When regex `extract_ticker()` returns empty, agents now call MCP `resolve_company_ticker` before giving up — handles "analyze Mastercard" → "MA" correctly
- **Fixed `re.IGNORECASE` bug in `extract_ticker()`**: Added `.isupper()` guard to prevent lowercase words ("the", "in") from being captured as tickers
- **Pattern 4 changed from `matches[-1]` to `matches[0]`**: The last-matching heuristic picked up trailing stop words ("SEC", "EPS") over the actual ticker. First-match prefers the ticker, which typically appears earlier in LLM-generated task text. Falls back to company name resolution if wrong.
- **`is_valid_ticker_format()` guard**: New shared function rejecting non-standard tickers (digits, periods, >5 chars). Applied in all agents' `_resolve_ticker()` to prevent mutual fund identifiers from reaching validation.
- **`_validate_ticker` fallback to resolution**: When regex-extracted ticker fails SEC validation, agents retry with company name resolution before returning an error.

## v1.4 — Ticker Extraction Decoupled from SEC Validation

- **`extract_ticker()` simplified**: `shared/ticker_utils.py` is now pure regex — no SEC API calls, no `httpx` dependency, instant execution. All SEC validation moved to MCP server.
- **`_validate_ticker()` added to all agents**: Consistent `tuple[bool, str, str]` return type across agent_2 (RAG), agent_3 (Quant), and agent_4 (Sentiment). Connects MCP, calls `validate_ticker` tool, falls back to regex guess on MCP failure.
- **Fixed broken `_connect()` call in agent_2**: `RAGAgent.stream()` was calling `await self._connect()` which didn't exist. Now uses `_validate_ticker()`.
- **Error message for missing ticker**: All agents now show: *"Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V)."*
- **MCP `validate_ticker` pre-warming**: SEC ticker map pre-loaded on first tool call (`_prewarm_ticker_map()`) so subsequent validations are instant.
- **All agents handle MCP downtime**: If MCP connect or `validate_ticker` call fails, agents log a warning and proceed with the regex-extracted ticker (best-effort).

## v1.3 — MCP Server Fixes & RAG Content Ingestion

- **Docker-compose MCP fix**: Replaced 4 broken MCP services (`mcp-yfinance`, `mcp-sec-edgar`, `mcp-reddit`, `mcp-python-runner`) that referenced non-existent files with single unified `finsight-mcp` service
- **New `get_filing_content` tool**: Added MCP tool to fetch and extract text from raw SEC EDGAR filing URLs with IXBRL fallback
- **RAG content ingestion fix**: RAG agent now fetches actual filing content (10-K, 10-Q, 8-K text) via `get_filing_content()` instead of just storing metadata. Returns raw document URL (`edgar_url`) plus viewer fallback (`ix_url`)
- **Quant DCF fix**: DCF valuation now correctly reads free cash flow from `cash_flow` statement instead of `income_statement` (was returning null)
- **MCP response parsing utility**: Added `parse_mcp_result()` to `shared/mcp_client.py` for consistent handling across all agents

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
