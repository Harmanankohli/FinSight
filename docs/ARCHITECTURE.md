# Architecture

## Overview

FinSight is a multi-agent investment research system where four specialized agents communicate via the **Google A2A Protocol** (Agent-to-Agent). The orchestrator (ADK `LlmAgent`) discovers sub-agents at startup via `A2ACardResolver`, delegates tasks via a single `send_message` tool, and the LLM routes to each agent sequentially. Each sub-agent processes tasks internally using its own framework and tools.

## Communication Pattern

```
┌──────────────────────────────────────────────────────────────┐
│                    A2A Protocol Layer                         │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Agent Card Discovery                                  │    │
│  │ GET /.well-known/agent-card.json                      │    │
│  │ ─ name, skills (id + description), interfaces         │    │
│  └──────────────────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ JSON-RPC over HTTP (streaming)                        │    │
│  │ POST /a2a  events: task, status_update, artifact_upd  │    │
│  │ Headers: A2A-Version: 1.0                             │    │
│  └──────────────────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Task Lifecycle                                        │    │
│  │ SUBMITTED → WORKING → (artifacts) → COMPLETED        │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

## Orchestrator Architecture

The orchestrator (`agent_1_adk/`) uses a single `LlmAgent` with one `send_message` tool that delegates to sub-agents via A2A:

```
Module load → SubAgentClient.discover()
  ├── A2ACardResolver(httpx.AsyncClient, url) per seed URL
  ├── Returns typed AgentCard (protobuf)
  └── self.agents populated → instruction updated

A2A Request → FinSightAgentExecutor.execute()
  → RUNNER.run_async(user_query)
  → LlmAgent (no pre-fetch)
    → LLM calls send_message(agent_name, task) for each agent
    → SubAgentClient → A2A task to sub-agent
    → SubAgentClient → A2A response (text or data parts)
    → LLM calls next agent
    → LLM synthesizes BUY/HOLD/SELL
  → COMPLETED with synthesis
```

**Key design choices:**

- **A2ACardResolver for discovery** — standard A2A well-known endpoint, proper protobuf AgentCard types, compatibility with legacy formats
- **ClientFactory for A2A communication** — matches the official A2A SDK pattern, supports multiple transport protocols
- **Single `send_message` tool** — not one tool per agent. LLM passes agent name as a parameter, matching all A2A sample projects
- **Streaming event handling** — correctly skips intermediate SUBMITTED/WORKING events, captures `artifact_update` (data/text) and terminal `status_update` events
- **Background async discovery** — supports both ADK Web UI (running event loop) and CLI entrypoints
- **WindowsSelectorEventLoopPolicy** — prevents ConnectionResetError noise on Windows
- **Programmatic AgentCards** — built in code using protobuf `AgentCard(...)` instead of static JSON files

## GenericAgentExecutor Pattern

All sub-agents extend `BaseAgent` and implement `stream()`. A shared `GenericAgentExecutor` handles A2A lifecycle:

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor.execute()
  → BaseAgent.stream(query, context_id, task_id)
  → Yields: {response_type, content, is_task_complete, require_user_input}
  → GenericAgentExecutor converts to TaskStatusUpdateEvent / TaskArtifactUpdateEvent
  → COMPLETED state
```

## Agent Architecture

### RAG Agent (LlamaIndex)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(RAGAgent)
  → RAGAgent.stream()
    → RAGAgent._ensure_ingested(ticker)
      ├── MCPClient.connect_all()
      ├── MCP: get_company_filings(ticker) → returns filings with edgar_url + ix_url
      ├── MCP: get_filing_content(edgar_url, ix_url) for each filing → raw text
      └── DocumentIngestionPipeline.ingest_sec_filings_batch() → ChromaDB
    → FinancialIndexManager.query(ticker, query)
      ├── Try: RouterQueryEngine
      └── Fallback: SEC filings index directly
  → Yields data response with summary + sources
```

**Content Ingestion**: The RAG agent now fetches actual SEC filing content (10-K, 10-Q, 8-K) via `get_filing_content()`, which extracts text from raw EDGAR URLs with fallback to IXBRL viewer URLs. This enables the RAG index to contain actual filing text rather than just metadata.

### Quant Agent (LangGraph)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(QuantAgent)
  → QuantAgent.stream()
    → extract_holdings(query) → portfolio_holdings list
    → analyze(ticker, portfolio_holdings=holdings)
      [MCP: get_prices → parse Close data]
      → compute_metrics → conditional branch (logged with ticker + volatility):
          high volatility (vol > 35%) → stress_test, dcf_error set
          low volatility  (vol ≤ 35%) → dcf_valuation [MCP: get_financials → cash_flow]
      → portfolio_correlation [MCP: get_prices per holding + target ticker]
      → format_output (dcf_error included in reasoning if DCF skipped)
      → llm_summary
  → Yields data response with dcf_error in result
```

**Portfolio Holdings Extraction**: `stream()` uses `extract_holdings(query, exclude_ticker=ticker)` from `shared/ticker_utils.py` to extract holdings from natural language (e.g. "My portfolio holds AAPL, MSFT, GOOGL"). The orchestrator LLM is instructed to include holdings in the task text for the Quant agent. Holdings are passed through the full chain: `stream()` → `analyze()` → `graph.run()` → `correlation_node`.

**Correlation Matrix Notes**: When no holdings are provided, returns `{"note": "No portfolio holdings provided..."}` instead of `{}`. When price data is insufficient or computation fails, returns a descriptive error.

**DCF Fix**: The DCF valuation now correctly reads free cash flow data from the `cash_flow` financial statement (not `income_statement`). This fixes the issue where DCF valuations were returning null.

**DCF Skip Messaging**: When annual volatility exceeds the 35% threshold, `compute_metrics_node` sets a descriptive `dcf_error` field (e.g. "DCF skipped: annual volatility (41.0%) exceeds 35% threshold – routed to stress test instead"). This error is propagated through the graph into the final output and LLM reasoning, providing visibility into why DCF was not computed.

### Sentiment Agent (CrewAI)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(SentimentAgent)
  → SentimentAgent.stream()
    → Parallel MCP data collection (asyncio.gather):
      ├── get_news_sentiment
      └── get_company_filings
    → 2-agent CrewAI: Analysis → Synthesis
  → Yields data response
```

## MCP Architecture

Single unified MCP server (`mcp_servers/finsight_server.py`, port 8010) hosting agent registry + all data tools:

```
┌──────────────────────────────────────────────────────┐
│              finsight-mcp (port 8010)                 │
│                                                       │
│  Agent Registry         │  Data Sources                    │
│  ├── find_agent()       │  ├── get_prices()                │
│  ├── resource://cards   │  ├── get_financials()            │
│  └── resource://{name}  │  ├── get_options_chain()         │
│                         │  ├── get_company_filings()       │
│                         │  ├── get_financial_filings()     │
│                         │  ├── get_filing_content()        │
│                         │  ├── validate_ticker()           │
│                         │  ├── resolve_company_ticker()    │
│                         │  ├── full_text_search()          │
│                         │  ├── get_news_sentiment()        │
│                         │  ├── get_earnings_calendar()     │
│                         │  └── execute_python()            │
└──────────────────────────────────────────────────────┘
```

Agent cards loaded from `agent_cards/*.json`, embedded via `sentence-transformers`, queried via `find_agent` tool using dot-product similarity.

## Timeout Architecture

Timeouts configured via `.env` with `A2A_TIMEOUT=300.0`:

| Layer | Timeout | Mechanism |
|---|---|---|
| A2A discovery | 10s per URL | httpx.AsyncClient within A2ACardResolver |
| A2A messaging | 300s | ClientConfig + httpx.AsyncClient |
| MCP tool calls | 30s | MCPClient default |

## Error Handling

| Failure Mode | Strategy |
|---|---|
| Agent discovery failure | Retries 3x with 5s delay |
| A2A timeout | Caught, returns error JSON to LLM |
| MCP connection failure | Exponential backoff (2^attempt), max 3 retries |
| Agent unavailable | Skipped, LLM works with what it has |
| Response parse failure | `json.JSONDecodeError` caught, text used as-is |

## LLM Configuration

All agents use LM Studio (OpenAI-compatible local API):

| Agent | Model | Provider |
|---|---|---|
| Orchestrator (ADK) | `qwen/qwen3-30b-a3b-2507` | `openai/` prefix (LM Studio endpoint) |
| RAG (LlamaIndex) | `qwen/qwen3-30b-a3b-2507` | `llama-index-llms-openai-like` |
| Quant (LangGraph) | `qwen/qwen3-30b-a3b-2507` | `langchain-openai` |
| Sentiment (CrewAI) | `qwen/qwen3-30b-a3b-2507` | CrewLLM (OpenAI-compatible) |

## Observability & Tracing

Langfuse provides distributed tracing across all four agent processes. Trace context is propagated through the A2A protocol via text-based injection.

### Trace Propagation Flow

```
Orchestrator (agent_1_adk)
  ├── agent_executor.py: langfuse.trace(name="finsight-query")
  ├── start_as_current_observation(name="orchestrator-execute")
  ├── send_message() tool: get_current_trace_id() + get_current_observation_id()
  └── inject_trace_context(task, trace_id, parent_span_id) → A2A text prefix

A2A Protocol (JSON-RPC over HTTP)
  └── Task text prefixed with {"_trace": {"trace_id": "...", "parent_span_id": "..."}}

Sub-agents (agent_2, agent_3, agent_4)
  ├── extract_trace_ids(query) → (trace_id, parent_span_id, clean_query)
  ├── start_observation(trace_context={"trace_id": ..., "parent_span_id": ...})
  └── Langfuse joins the span to the orchestrator's trace tree
```

### Trace Context Utility

| Function | File | Purpose |
|---|---|---|
| `inject_trace_context(task, trace_id, parent_span_id)` | `shared/trace_context.py` | Serializes trace IDs as JSON prefix in task text |
| `extract_trace_context(task)` | `shared/trace_context.py` | Returns `(trace_ctx_dict, clean_query)` |
| `extract_trace_ids(task)` | `shared/trace_context.py` | Returns `(trace_id, parent_span_id, clean_query)` |

### Span Noise Filtering

The Langfuse client uses `is_default_export_span` to filter out noisy A2A internal spans (`a2a-python-sdk` scope) and HTTPX transport spans. Only high-level workflow spans (`langfuse-sdk` scope) and LLM spans are exported, keeping traces clean and focused.

### Per-Agent Instrumentation

| Agent | Service Name | Instrumentors | Manual Spans |
|---|---|---|---|
| Orchestrator | `orchestrator` | GoogleADKInstrumentor, HTTPXClientInstrumentor | `orchestrator-execute` span |
| RAG Agent | `rag_agent` | LlamaIndexInstrumentor | `rag-agent-stream` span |
| Quant Agent | `quant_agent` | StarletteInstrumentor | `quant-agent-stream` span + CallbackHandler for LangGraph nodes |
| Sentiment Agent | `sentiment_agent` | CrewAIInstrumentor, StarletteInstrumentor | `sentiment-agent-stream` span |
| MCP Server | `mcp_server` | — | `@observe()` on individual tools |
