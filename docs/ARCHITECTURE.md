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
    [MCP: get_prices → parse Close data]
    → compute_metrics → conditional branch:
        high volatility → stress_test
        low volatility  → dcf_valuation [MCP: get_financials]
    → portfolio_correlation [MCP: get_prices per holding]
    → format_output → llm_summary
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
| Orchestrator (ADK) | `gpt-oss-20b` | `openai/` prefix (LM Studio endpoint) |
| RAG (LlamaIndex) | `gpt-oss-20b` | `llama-index-llms-openai-like` |
| Quant (LangGraph) | `gpt-oss-20b` | `langchain-openai` |
| Sentiment (CrewAI) | `gpt-oss-20b` | CrewLLM (OpenAI-compatible) |
