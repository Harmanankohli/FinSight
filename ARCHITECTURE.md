# Architecture

## Overview

FinSight is a multi-agent system where four specialized agents communicate via the Google A2A Protocol v1.0. The orchestrator (Google ADK) discovers agents dynamically via their agent cards, dispatches sub-tasks based on skill matching, and synthesizes results into a structured Investment Brief.

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
│  │ Response: {result: {task: {status, artifacts}}}       │    │
│  └──────────────────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Task Lifecycle                                        │    │
│  │ SUBMITTED → WORKING → (INPUT_REQUIRED) → COMPLETED   │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

### Discovery Flow

1. Orchestrator knows seed agent URLs (from `AGENT_SEED_URLS` env var)
2. On startup, fetches each agent's card from `/.well-known/agent-card.json`
3. Cards contain skill IDs, descriptions, and interface bindings
4. Orchestrator builds a registry: skill_id → agent_url
5. When query comes in, LLM decides which skills to invoke

### A2A v1.0 Requirements

The SDK (`a2a-sdk`) enforces several requirements:

| Requirement | What we use |
|---|---|
| Method name | `SendMessage` (not `tasks/send`) |
| Role enum | `ROLE_USER` (not `user`) |
| messageId | Required UUID on every Message |
| Version header | `A2A-Version: 1.0` |
| Agent card interfaces | `protocol_binding: "JSONRPC"` |
| Response structure | `result.task.artifacts[].parts[].data` |

## Agent Architecture

### RAG Agent (LlamaIndex)

```
A2A Request → DefaultRequestHandler → RAGAgentExecutor.execute()
  → RAGAgent._ensure_ingested()      # Fetches SEC filings via MCP
  → FinancialIndexManager.query()     # ChromaDB + Ollama LLM
    ├── Try: RouterQueryEngine        # Falls back if JSON parsing fails
    └── Fallback: SEC filings index   # Direct query
  → Response with summary + sources
```

**Why LlamaIndex for RAG**: LlamaIndex's `RouterQueryEngine` with `LLMMultiSelector` provides automatic index selection based on query intent. The `VectorStoreIndex` + ChromaDB integration is straightforward. The `SentenceSplitter` node parser handles chunking at 512 tokens with 50-token overlap.

**What we learned**: The `RouterQueryEngine` with Groq's LLM generated malformed JSON for the routing decision. We added a fallback that queries the SEC filings index directly if the router fails.

### Quant Agent (LangGraph)

```
A2A Request → QuantAgentExecutor.execute()
  → fetch_prices            # yfinance OHLCV
  → compute_metrics         # Sharpe, Beta, VaR, Volatility
  → conditional branch:
      high volatility → stress_test    # 4 crash scenarios
      low volatility  → dcf_valuation  # Discounted cash flow
  → portfolio_correlation  # vs benchmark + holdings
  → format_output          # Signal + confidence
  → llm_summary            # Ollama natural language summary
```

**Why LangGraph**: The conditional branching (high volatility → stress test vs DCF) is a natural fit for LangGraph's `StateGraph`. The `add_conditional_edges` API makes the routing explicit. The `RedisCheckpointer` enables resume on failure.

### Sentiment Agent (CrewAI)

```
A2A Request → SentimentAgentExecutor.execute()
  → Parallel MCP data collection (asyncio.gather):
    ├── get_news_sentiment    (financial-news MCP :8025)
    └── get_company_filings   (SEC EDGAR MCP :8020)
  → 2-agent CrewAI:
    ├── Analysis Agent        # Extracts signals from data
    └── Synthesis Agent       # Writes investment narrative
```

**Why parallel data collection**: The original design used 4 CrewAI agents making sequential tool calls. This caused two issues: (a) rate limit violations with Groq, and (b) LLM hallucinating non-existent tool names. By pre-collecting data in parallel and passing it as context, we eliminated tool calling from the CrewAI workflow entirely.

## MCP Architecture

```
MCPClient (shared/mcp_client.py)
  ├── connect_all()           # SSE connection to each server
  ├── list_tools()            # Dynamic discovery via MCP protocol
  ├── call_tool_by_name()     # Routes by tool name (not server)
  └── _tool_registry          # tool_name → server_name mapping
```

Each MCP server is a `FastMCP` SSE server. Tools are defined with `@app.tool()` decorators. On connect, the client calls `session.list_tools()` to discover available tools, then builds a registry mapping tool names to server names. This means agents call `call_tool_by_name("get_prices", {...})` without knowing which server hosts it.

## Error Handling

| Failure Mode | Strategy |
|---|---|
| A2A timeout | 30s timeout, retry 2x, proceed with partial results |
| MCP connection failure | Exponential backoff (2^attempt), 3 retries |
| Agent unavailable | Orchestrator proceeds with available agents |
| LLM routing failure | Fallback to direct index query |
| Rate limit (Groq) | Switched to local Ollama |

## LLM Configuration

All agents can run with either local Ollama or Groq API:

| Agent | Default LLM | Fallback |
|---|---|---|
| RAG (LlamaIndex) | Ollama llama3.2 via `llama-index-llms-ollama` | — |
| Quant (LangGraph) | Ollama llama3.2 via `langchain-ollama` | — |
| Sentiment (CrewAI) | Ollama llama3.2 via litellm | Groq llama-3.1-8b-instant |
| ADK Web (Orchestrator) | Ollama llama3.2 via `openai/` prefix | — |
