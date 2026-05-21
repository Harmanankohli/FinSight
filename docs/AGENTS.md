# Agents Reference

## Agent 1: Orchestrator (ADK)

| Property | Value |
|---|---|
| Framework | Google ADK |
| Port | 8001 |
| Agent Card | Built programmatically in `agent_1_adk/main.py` |
| Discovery | `A2ACardResolver` via `/.well-known/agent-card.json`, async with 3x retry |
| A2A Endpoint | `POST /a2a` (via Starlette + `create_jsonrpc_routes`) |

The orchestrator uses a single `LlmAgent` with one `send_message` tool. The LLM delegates tasks to sub-agents by name and synthesizes results:

1. **Discovers agents in background** via `SubAgentClient.discover()` — async `A2ACardResolver` with retries
2. **Memory context injection** — Before each query, extracts ticker from user input, retrieves latest recommendation from `TickerMemory`, and prepends it to the user message (~300 token budget)
3. **LLM routes to agents** — LLM calls `send_message(agent_name, task)` for each sub-agent. Parallel with qwen (supports parallel tool calls); sequential with other models.
4. **Synthesizes results** — LLM collects all outputs and produces a BUY/HOLD/SELL recommendation
5. **Auto-save** — After each response, persists ticker brief, portfolio holdings, and performance record to SQLite

All A2A communication uses `ClientFactory` + `BaseClient` from the official `a2a-sdk`. Streaming events are handled correctly: intermediate SUBMITTED/WORKING events are skipped, only `artifact_update` events (data or text) and terminal `status_update` events are returned to the LLM.

### Architecture

```
Module load → SubAgentClient.discover() in background task
  ├── A2ACardResolver(http, "http://localhost:8002")
  ├── A2ACardResolver(http, "http://localhost:8003")
  └── A2ACardResolver(http, "http://localhost:8004")
  → self.agents populated → instruction updated

FinSightAgentExecutor:
  A2A Request → execute()
  → _build_memory_context(query) → inject [MEMORY CONTEXT] prefix
  → RUNNER.run_async(user_query)
  → ADK LlmAgent (tools: [send_message, save_brief, load_memory])
    → LLM decides which agents to call (parallel with qwen)
    → send_message("Financial RAG Agent", "Analyze NVDA...")
    → send_message("Quant Analysis Agent", "Compute metrics for NVDA...")
    → send_message("Sentiment Intelligence Agent", "Sentiment for NVDA...")
    → LLM synthesizes BUY/HOLD/SELL
    → load_memory(query="...") — search past conversations
  → _add_events_to_memory() → get_session() → add_session_to_memory()
  → _persist_to_memory() → direct events → SQLiteMemoryService (for load_memory)
  → _store_memory() → TickerMemory + PortfolioStore + PerformanceTracker

after_agent_callback (ADK web UI path):
  → callback_context.add_events_to_memory(events=session.events, custom_metadata={...})
  → SQLiteMemoryService.add_events_to_memory() → memory_entries table
```

### Streaming Event Flow

When `send_message` is called, sub-agents respond with streaming events:

```
event 1: task { state: SUBMITTED }          ← skipped (non-terminal)
event 2: status_update { WORKING, msg }     ← skipped (non-terminal)
event 3: artifact_update { data: {...} }    ← returned (actual result)
    OR status_update { COMPLETED, msg }     ← returned (terminal text)
    OR task { state: COMPLETED, artifacts } ← returned (non-streaming fallback)
```

### Sample Request

```json
POST /a2a
Headers: {"A2A-Version": "1.0"}
Body:
{
  "jsonrpc": "2.0",
  "method": "SendMessage",
  "id": "abc123",
  "params": {
    "message": {
      "messageId": "def456",
      "role": "ROLE_USER",
      "parts": [{"text": "Should I invest in NVDA?"}]
    }
  }
}
```

---

## Agent 2: RAG (LlamaIndex)

| Property | Value |
|---|---|
| Framework | LlamaIndex |
| Executor | `GenericAgentExecutor(RAGAgent)` |
| LLM | LM Studio via `llama-index-llms-openai-like` |
| Vector Store | ChromaDB (local, persisted to `./chroma_db`) |
| Embeddings | HuggingFace `all-MiniLM-L6-v2` (local) |
| Port | 8002 |
| Agent Card | Built programmatically in `agent_2_llamaindex/server.py` |
| A2A Endpoint | `POST /a2a` |

### Skills

| ID | Name | Description |
|---|---|---|
| `sec_filing_retrieval` | SEC Filing Retrieval | Retrieves and analyzes SEC 10-K, 10-Q, 8-K filings |
| `earnings_summary` | Earnings Summary | Summarizes earnings call transcripts |

### Architecture

```
Request → DefaultRequestHandler → GenericAgentExecutor(RAGAgent)
  → RAGAgent.stream(query, context_id, task_id)
    → RAGAgent._ensure_ingested(ticker)
      ├── MCPClient.connect_all()
      └── MCPClient.call_tool_by_name("get_company_filings", {...})
    → FinancialIndexManager.query(ticker, query)
      ├── Try: RouterQueryEngine → LM Studio LLM → response
      └── Fallback: SEC filings index directly
  → Yields: {response_type: "data", content: result, is_task_complete: true}
```

---

## Agent 3: Quant (LangGraph)

| Property | Value |
|---|---|
| Framework | LangChain + LangGraph |
| Executor | `GenericAgentExecutor(QuantAgent)` |
| LLM | LM Studio via `langchain-openai` (summary node only) |
| Data Source | MCP (finsight-mcp `get_prices`, `get_financials`) |
| Port | 8003 |
| Agent Card | Built programmatically in `agent_3_langgraph/server.py` |
| A2A Endpoint | `POST /a2a` |

### Skills

| ID | Name | Description |
|---|---|---|
| `quant_analysis` | Quantitative Analysis | Compute Sharpe ratio, Beta, VaR, volatility, DCF valuation, stress tests |

### Architecture

```
Request → DefaultRequestHandler → GenericAgentExecutor(QuantAgent)
  → QuantAgent.stream(query, context_id, task_id)
    → extract_holdings(query, exclude_ticker=ticker) → ["AAPL", "MSFT", "GOOGL"]
    → QuantAgent.analyze(ticker, portfolio_holdings=holdings)
      [MCP: get_prices → parse Close data] → compute_metrics → conditional branch (logged)
        ├── high_volatility (annual_vol > 35%) → stress_test, sets dcf_error
        └── low_volatility (annual_vol ≤ 35%) → dcf_valuation
      → portfolio_correlation (fetches prices for target + each holding)
        → format_output (dcf_error in reasoning) → llm_summary
  → Yields: {response_type: "data", content: result, is_task_complete: true}
      Result includes dcf_error field when DCF is skipped
```

**Portfolio Holdings Extraction**: `extract_holdings()` in `shared/ticker_utils.py` uses regex patterns to extract holdings from natural language (e.g. "My portfolio holds AAPL, MSFT, GOOGL"). The orchestrator LLM is instructed to include holdings in the task text for the Quant agent. Holdings are passed through the full chain and used by `correlation_node` to compute a correlation matrix.

**Correlation Matrix Notes**: When no holdings are provided, returns `{"note": "No portfolio holdings provided..."}` instead of `{}`.

**Logging & Error Propagation**: The graph logs routing decisions, metric computation failures, DCF fallbacks, and beta calculation errors. When DCF is skipped due to high volatility, the `dcf_error` field is set and propagated through formatting and LLM summary, giving users visibility into why DCF was not computed.

---

## Agent 4: Sentiment (CrewAI)

| Property | Value |
|---|---|
| Framework | CrewAI |
| Executor | `GenericAgentExecutor(SentimentAgent)` |
| LLM | LM Studio via CrewLLM |
| Data Collection | Parallel via `asyncio.gather` |
| Port | 8004 |
| Agent Card | Built programmatically in `agent_4_crewai/server.py` |
| A2A Endpoint | `POST /a2a` |

### Skills

| ID | Name | Description |
|---|---|---|
| `sentiment_analysis` | Sentiment & Narrative Intelligence | Analyze news sentiment, SEC filings, produce investment narrative |

### Architecture

```
Request → DefaultRequestHandler → GenericAgentExecutor(SentimentAgent)
  → SentimentAgent.stream(query, context_id, task_id)
    → SentimentAgent.analyze(ticker)
      ├── _connect() → MCPClient.connect_all()
      ├── _collect_data_parallel(ticker)
      │   ├── call("get_news_sentiment", {ticker})
      │   ├── call("get_company_filings", {ticker})
      │   └── asyncio.gather
      └── SentimentIntelligenceCrew.analyze(ticker, precollected_data)
          ├── Analysis Agent
          └── Synthesis Agent
  → Yields: {response_type: "data", content: result, is_task_complete: true}
```
