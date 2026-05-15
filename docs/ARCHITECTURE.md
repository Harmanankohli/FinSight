# Architecture

## Overview

FinSight is a multi-agent investment research system where four specialized agents communicate via the **Google A2A Protocol**. The orchestrator (ADK `LlmAgent`) discovers sub-agents at startup, generates one ADK tool per agent, and delegates tasks via A2A. Each sub-agent processes tasks internally using its own framework and tools.

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
│  │ JSON-RPC over HTTP                                    │    │
│  │ POST /a2a  {"method":"SendMessage","params":{...}}    │    │
│  │ Headers: A2A-Version: 1.0                             │    │
│  └──────────────────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Task Lifecycle                                        │    │
│  │ SUBMITTED → WORKING → (INPUT_REQUIRED) → COMPLETED   │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

## Orchestrator Architecture

The orchestrator (`agent_1_adk/`) follows a discover → generate → delegate pattern:

```
Module load → SubAgentClient.discover_sync()
  ├── httpx.Client.get(/.well-known/agent-card.json) per seed URL
  ├── Retries failed URLs up to 3x with 5s delay
  └── _agent_list populated

LlmAgent construction
  ├── _make_agent_tool(name, desc) → one ADK tool per agent
  ├── Instruction lists all available agents
  └── tools=[financial_rag_agent, quant_analysis_agent, ...]

At runtime (LLM tool call)
  → _get_a2a_client(agent_name)  # lazy, cached
    → create_client(url)         # A2A SDK — creates httpx + A2A client
    → ClientConfig with proper timeout via ClientConfig + httpx client
  → client.send_message(req, context=ClientCallContext(timeout=300))
  → Iterate StreamResponse events
  → _extract_text(task)          # text or MessageToDict for data parts
```

**Key design choices:**

- **Sync discovery** (not async): Avoids `asyncio.run()` conflicts with ADK Web UI's running event loop
- **Lazy A2A clients**: Created on first tool call in the correct async event loop
- **Explicit timeouts**: Both `ClientConfig` and `ClientCallContext` propagate the 300s timeout
- **Response extraction**: Handles both `text` and `data` (protobuf Struct via `MessageToDict`) responses

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
      └── MCPClient.call_tool_by_name("get_company_filings", {...})
    → FinancialIndexManager.query(ticker, query)
      ├── Try: RouterQueryEngine
      └── Fallback: SEC filings index directly
  → Yields data response with summary + sources
```

### Quant Agent (LangGraph)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(QuantAgent)
  → QuantAgent.stream()
    → fetch_prices → compute_metrics → conditional branch:
        high volatility → stress_test
        low volatility  → dcf_valuation
    → portfolio_correlation → format_output → llm_summary
  → Yields data response
```

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
│  Agent Registry         │  Data Sources               │
│  ├── find_agent()       │  ├── get_prices()           │
│  ├── resource://cards   │  ├── get_financials()       │
│  └── resource://{name}  │  ├── get_options_chain()    │
│                         │  ├── get_company_filings()  │
│                         │  ├── full_text_search()     │
│                         │  ├── get_news_sentiment()   │
│                         │  ├── get_earnings_calendar()│
│                         │  └── execute_python()       │
└──────────────────────────────────────────────────────┘
```

Agent cards loaded from `agent_cards/*.json`, embedded via `sentence-transformers`, queried via `find_agent` tool using dot-product similarity.

## Timeout Architecture

Timeouts configured via `.env` with `A2A_TIMEOUT=300.0`:

| Layer | Timeout | Mechanism |
|---|---|---|
| Sync discovery | 10s per URL | httpx.Client timeout |
| A2A messaging | 300s | ClientConfig + httpx.AsyncClient |
| Per-call timeout | 300s | ClientCallContext |
| MCP tool calls | 30s | MCPClient default |

## Error Handling

| Failure Mode | Strategy |
|---|---|
| Agent discovery failure | Retries 3x with 5s delay |
| A2A timeout | Proceed with partial results |
| MCP connection failure | Exponential backoff (2^attempt), max 3 retries |
| Agent unavailable | Skipped, LLM works with what it has |
| Response parse failure | `json.JSONDecodeError` caught, text used as-is |

## LLM Configuration

Agent run with Ollama:

| Agent | Model | Provider |
|---|---|---|
| Orchestrator (ADK) | `qwen2.5:7b` | `openai/` prefix (Ollama endpoint) |
| RAG (LlamaIndex) | `qwen2.5:7b` | `llama-index-llms-ollama` |
| Quant (LangGraph) | `qwen2.5:7b` | `langchain-ollama` |
| Sentiment (CrewAI) | `qwen2.5:7b` | litellm via Ollama |
