# Plan: Add DuckDuckGo Web Search to FinSight Agents

## Context

The system's agents currently rely on structured data APIs (yfinance, SEC EDGAR, RSS feeds) for all information. This leaves a gap in qualitative context — analyst commentary, regulatory updates, sector narratives, earnings call interpretations, and event catalysts. A `web_search` MCP tool (DuckDuckGo `DDGS.text()`) already exists from a partial implementation (`src/mcp_tools/tools/web_search.py`). The infrastructure (rate limiter, cache, tool registration, pyproject dependency) is in place. What remains is wiring the tool into the agents that benefit from it.

**Priority ranking by impact:**
1. **Market Context Agent** — Very High (missing macro commentary, sector narratives, competitive context)
2. **Quant Agent** — High (missing analyst rationale, insider context, options flow catalysts)  
3. **RAG Agent** — Medium (could augment filing context with recent news/events)
4. **Analytics Agent** — Low (could explain detected anomalies)
5. **Reviewer Agent** — Skip (pure validator, should not consume external data)

---

## Phase 1: MCP Tool Finalization

The `web_search` MCP tool file, rate limiter, cache, `__init__.py` registration, and `duckduckgo-search` dependency already exist. This phase verifies and finalizes them.

### 1A: Verify existing MCP tool
- **File**: `src/mcp_tools/tools/web_search.py` — already complete
- **File**: `src/mcp_tools/infra/rate_limiters.py` — `_WEB_SEARCH_LIMITER` and `cache_web_search` already added
- **File**: `src/mcp_tools/tools/__init__.py` — `web_search` import already added
- **File**: `pyproject.toml` — `duckduckgo-search>=7.0.0` already in dependencies
- **Action**: Run `uv sync` to ensure lockfile is updated, then smoke-test import:
  ```
  PYTHONPATH=src uv run python -c "from mcp_tools.tools.web_search import web_search; print('OK')"
  ```

### 1B: Add unit test for web_search tool
- **New file**: `src/tests/unit/test_web_search_tool.py`
- Test `_web_search_uncached` with a mocked `DDGS` class
- Test result structure: `query`, `total_results`, `results` list, `timestamp`
- Test graceful fallback when `duckduckgo-search` is not installed (ImportError path)
- Test `max_results` clamping (1–20) and `time_filter` validation

---

## Phase 2: Market Context Agent Integration

The Market Context agent collects data in `_collect_data_parallel()` (`src/market_context/executor.py:31`) then passes it to the CrewAI crew. Web search results should be fetched alongside macro/peers and injected into the crew's context data.

### 2A: Add web search to data collection
- **File**: `src/market_context/executor.py` — `_collect_data_parallel()`
- Add a `web_search` MCP call in the Step 1 `asyncio.gather` (parallel with macro + financials):
  ```python
  web_context = await call("web_search", {
      "query": f"{ticker} stock market analysis outlook {sector}",
      "max_results": 5,
      "time_filter": "w",
  })
  ```
- Add a second web search for macro context (parallel with the first):
  ```python
  macro_web = await call("web_search", {
      "query": f"{sector} sector outlook macro risks analyst commentary",
      "max_results": 5,
      "time_filter": "w",
  })
  ```
- Return both in the data dict: `"web_context": web_context, "macro_web_context": macro_web`

### 2B: Inject web search results into crew context
- **File**: `src/market_context/crew.py` — `build_crew()`
- Extract web search snippets from `data.get("web_context")` and `data.get("macro_web_context")`
- Format as a `WEB SEARCH CONTEXT` section appended to `context_data`:
  ```
  WEB SEARCH — COMPANY CONTEXT:
  - [title]: snippet (source: url)
  ...
  
  WEB SEARCH — MACRO/SECTOR CONTEXT:
  - [title]: snippet (source: url)
  ...
  ```
- Truncate total web context to ~2000 chars to avoid blowing the LLM context window

### 2C: Add web context to retrieved contexts for eval
- **File**: `src/market_context/executor.py` — `_extract_retrieved_contexts()`
- Append web search snippets to the contexts list so RAGAS Faithfulness can validate grounding

---

## Phase 3: Quant Agent Integration

The Quant agent uses a LangGraph pipeline with nodes fetching data via MCP. Web search should be added as a new data-fetch step providing qualitative context for the LLM summary node.

### 3A: Add web search data fetch function
- **File**: `src/quant/nodes/data_fetch.py`
- Add new async function `fetch_web_context_node(state: QuantAnalysisState) -> dict`:
  - Calls `mcp.call_tool_by_name("web_search", {"query": f"{ticker} stock analyst opinion news", "max_results": 5, "time_filter": "w"})`
  - Parses results into a list of `{title, snippet}` dicts
  - Returns `{"web_context": results_list}`

### 3B: Add web_context to graph state
- **File**: `src/quant/state.py` (or wherever `QuantAnalysisState` is defined)
- Add `web_context: list[dict]` field with `default_factory=list`

### 3C: Wire into the LangGraph pipeline
- **File**: `src/quant/graph.py`
- Add `fetch_web_context_node` to the data-fetch phase (parallel with existing fetch nodes)
- The node runs in parallel with `fetch_price_data_node` and `fundamental_analysis_node`

### 3D: Use web context in LLM summary
- **File**: `src/quant/nodes/summary.py`
- In the prompt construction for the LLM summary, append web context snippets as additional context:
  ```
  WEB CONTEXT (recent analyst/news):
  - {title}: {snippet}
  ...
  ```
- This gives the LLM narrative context for interpreting quantitative signals

---

## Phase 4: RAG Agent Integration

The RAG agent retrieves SEC filings and news sentiment. Web search can augment the news context, especially when RSS feeds return sparse results.

### 4A: Add web search after news sentiment fetch
- **File**: `src/financial_rag/executor.py`
- After the existing `get_news_sentiment` call (~line 131), add a `web_search` call:
  ```python
  web_results = await mcp.call_tool_by_name("web_search", {
      "query": f"{ticker} {company} recent news analysis",
      "max_results": 5,
      "time_filter": "w",
  })
  ```
- Parse web results and merge snippets into the `news_items` or `context_texts` that feed into the RAG synthesis

### 4B: Include web results in retrieved contexts
- Append web search snippets to `retrieved_contexts` so they're available for RAGAS eval and the LLM summary

---

## Phase 5: Analytics Agent Integration (Optional)

Low priority — only useful for anomaly explanation.

### 5A: Add web search for anomaly context
- **File**: `src/analytics/nodes/anomaly.py`
- After detecting anomalies, if severity >= "medium", call `web_search` to find the catalyst:
  ```python
  web_search("AAPL price spike {anomaly_date} news catalyst", max_results=3, time_filter="w")
  ```
- Store the catalyst snippets in the anomaly report as `catalyst_context: list[str]`

### 5B: Update AnomalyReport model (if adding catalyst_context)
- **File**: `src/shared/agent_models.py`
- Add `catalyst_context: list[str] = Field(default_factory=list)` to `AnomalyReport`

---

## Phase 6: Orchestrator Preamble Update

Update the orchestrator's agent descriptions so the LLM knows web search context is available.

### 6A: Update agent responsibility boundaries
- **File**: `src/orchestrator/agent.py` — `_build_instruction()`
- Add to Market Context Agent description: "...and web search context for macro/sector commentary"
- Add to Quant Agent description: "...with web context for analyst opinions and news catalysts"

---

## Verification

1. **Unit test**: `uv run pytest src/tests/unit/test_web_search_tool.py -v` — mocked DDG test passes
2. **MCP smoke test**: Start MCP server, call `web_search` tool directly via MCP client
3. **Market Context integration test**: Run Market Context agent for a ticker, verify `web_context` appears in the crew's context data and output narrative references web-sourced information
4. **Quant integration test**: Run Quant agent, verify `web_context` is populated in state and LLM summary references analyst/news context
5. **RAG integration test**: Run RAG agent, verify web results appear in `context_texts`
6. **Full pipeline test**: Run full analysis via orchestrator, confirm all agents complete with web search data flowing through

---

## Files Modified Summary

| Phase | File | Change |
|-------|------|--------|
| 1A | (already done) | Verify existing tool/infra |
| 1B | `src/tests/unit/test_web_search_tool.py` | New unit test |
| 2A | `src/market_context/executor.py` | Add web_search calls to `_collect_data_parallel()` |
| 2B | `src/market_context/crew.py` | Inject web context into crew task description |
| 2C | `src/market_context/executor.py` | Add web snippets to `_extract_retrieved_contexts()` |
| 3A | `src/quant/nodes/data_fetch.py` | New `fetch_web_context_node()` |
| 3B | `src/quant/state.py` or equivalent | Add `web_context` field |
| 3C | `src/quant/graph.py` | Wire new node into pipeline |
| 3D | `src/quant/nodes/summary.py` | Include web context in LLM prompt |
| 4A | `src/financial_rag/executor.py` | Add web_search call after news fetch |
| 4B | `src/financial_rag/executor.py` | Merge web results into retrieved_contexts |
| 5A | `src/analytics/nodes/anomaly.py` | Web search for anomaly catalysts (optional) |
| 5B | `src/shared/agent_models.py` | Add `catalyst_context` to AnomalyReport (optional) |
| 6A | `src/orchestrator/agent.py` | Update agent descriptions |
