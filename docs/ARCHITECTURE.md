# Architecture

## Overview

FinSight is a multi-agent investment research system where four specialized agents communicate via the **Google A2A Protocol v1.0**. Agents are discovered via an **MCP-based agent registry** that hosts declarative agent card JSON files. The orchestrator decomposes queries into ordered tasks, routes each task to the right agent via skill-based discovery, and synthesizes results into a structured `InvestmentBrief`.

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

Two discovery modes are supported:

**1. MCP Registry (primary):** An MCP server (`mcp_servers/agent_registry_server.py`) hosts agent card JSONs from `agent_cards/`. The orchestrator queries resources via MCP to list and retrieve cards. Embedding-based `find_agent` tool enables semantic skill matching.

**2. Seed URLs (fallback):** The orchestrator knows agent URLs from `AGENT_SEED_URLS` env var and fetches cards directly via `GET /.well-known/agent-card.json`.

### Agent Card Registry

Agent cards are stored as declarative JSON files in `agent_cards/`:

| File | Agent | Skills |
|---|---|---|
| `orchestrator_agent.json` | Investment Orchestrator | `investment_research` |
| `rag_agent.json` | Financial RAG Agent | `sec_filing_retrieval`, `earnings_summary` |
| `quant_agent.json` | Quant Analysis Agent | `quant_analysis` |
| `sentiment_agent.json` | Sentiment Intelligence Agent | `sentiment_analysis` |

The registry MCP server loads these files, generates embeddings via `sentence-transformers`, and exposes:
- `find_agent(query)` — finds the best agent for a natural language task description
- `resource://agent_cards/list` — lists all available cards
- `resource://agent_cards/{name}` — retrieves a specific card

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

## GenericAgentExecutor Pattern

Instead of duplicated A2A event plumbing per agent, all agents extend `BaseAgent` and implement a single `stream()` method. A shared `GenericAgentExecutor` handles all A2A lifecycle events:

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor.execute()
  → BaseAgent.stream(query, context_id, task_id)
  → Yields: {response_type, content, is_task_complete, require_user_input}
  → GenericAgentExecutor converts to TaskStatusUpdateEvent / TaskArtifactUpdateEvent
  → COMPLETED state
```

**Why this pattern**: Eliminates ~100 lines of duplicated A2A boilerplate per agent (3 executors × 100 = 300 lines saved). Agents focus on business logic, not protocol plumbing.

```
Shared Modules:
├── shared/base_agent.py        # Abstract base class with stream() contract
├── shared/generic_executor.py  # Single AgentExecutor for all agents
├── shared/workflow.py          # WorkflowGraph state machine
├── shared/types.py             # Pydantic models (PlannerTask, TaskList, etc.)
├── shared/mcp_client.py        # MCP client for tool access
└── shared/config.py            # Centralized configuration
```

## Agent Architecture

### RAG Agent (LlamaIndex)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(RAGAgent)
  → RAGAgent.stream()
    → RAGAgent._ensure_ingested()      # Fetches SEC filings via MCP
    → FinancialIndexManager.query()     # ChromaDB + Ollama LLM
      ├── Try: RouterQueryEngine        # Falls back if JSON parsing fails
      └── Fallback: SEC filings index   # Direct query
  → Yields data response with summary + sources
```

**Why LlamaIndex for RAG**: LlamaIndex's `RouterQueryEngine` with `LLMMultiSelector` provides automatic index selection based on query intent. The `VectorStoreIndex` + ChromaDB integration is straightforward. The `SentenceSplitter` node parser handles chunking at 512 tokens with 50-token overlap.

**What we learned**: The `RouterQueryEngine` with Groq's LLM generated malformed JSON for the routing decision. We added a fallback that queries the SEC filings index directly if the router fails.

### Quant Agent (LangGraph)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(QuantAgent)
  → QuantAgent.stream()
    → fetch_prices            # yfinance OHLCV
    → compute_metrics         # Sharpe, Beta, VaR, Volatility
    → conditional branch:
        high volatility → stress_test    # 4 crash scenarios
        low volatility  → dcf_valuation  # Discounted cash flow
    → portfolio_correlation  # vs benchmark + holdings
    → format_output          # Signal + confidence
    → llm_summary            # Ollama natural language summary
  → Yields data response
```

**Why LangGraph**: The conditional branching (high volatility → stress test vs DCF) is a natural fit for LangGraph's `StateGraph`. The `add_conditional_edges` API makes the routing explicit.

### Sentiment Agent (CrewAI)

```
A2A Request → DefaultRequestHandler → GenericAgentExecutor(SentimentAgent)
  → SentimentAgent.stream()
    → Parallel MCP data collection (asyncio.gather):
      ├── get_news_sentiment    (financial-news MCP :8025)
      └── get_company_filings   (SEC EDGAR MCP :8020)
    → 2-agent CrewAI:
      ├── Analysis Agent        # Extracts signals from data
      └── Synthesis Agent       # Writes investment narrative
  → Yields data response
```

**Why parallel data collection**: By pre-collecting data in parallel and passing it as context, we eliminated tool calling from the CrewAI workflow entirely, avoiding LLM hallucination of non-existent tool names.

## Orchestration Pattern

The orchestrator (`agent_1_adk/gateway.py`) uses a planner → execute → synthesize pattern:

```
User Query → Planner decomposes into ordered tasks
  ├── Task 1: sec_filing_retrieval (ticker=NVDA)
  ├── Task 2: quant_analysis (ticker=NVDA)
  └── Task 3: sentiment_analysis (ticker=NVDA)
→ For each task:
    ├── Discover agent via A2ADiscoverer (MCP registry or seed URLs)
    ├── Execute via A2AClient.send_message(skill_id, query, metadata)
    └── Collect result
→ Synthesize all results into InvestmentBrief
→ Return JSON response
```

## MCP Architecture

### Single Unified MCP Server

Following the a2a_mcp reference pattern, FinSight uses a **single MCP server** (`mcp_servers/finsight_server.py`) that hosts all tools and the agent registry:

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

Agent cards are loaded from `agent_cards/*.json`, embedded via `sentence-transformers`, and queried via the `find_agent` tool using dot-product similarity.

```
MCPClient (shared/mcp_client.py)
  ├── connect_all()           # SSE connection to single server
  ├── list_tools()            # Dynamic discovery via MCP protocol
  ├── call_tool_by_name()     # Routes by tool name
  └── _tool_registry          # tool_name → server_name mapping
```

## Error Handling

| Failure Mode | Strategy |
|---|---|
| A2A timeout | `A2A_TIMEOUT` (default 45s), proceed with partial results |
| MCP connection failure | Exponential backoff (2^attempt), `MCP_MAX_RETRIES` (default 3) |
| Agent unavailable | Orchestrator proceeds with available agents, continues to next task |
| LLM routing failure | Fallback to direct index query |
| Discovery failure | Falls back from MCP registry to seed URLs |

## LLM Configuration

All agents run with local Ollama:

| Agent | Default LLM | Fallback |
|---|---|---|
| RAG (LlamaIndex) | Ollama ministral-3 via `llama-index-llms-ollama` | — |
| Quant (LangGraph) | Ollama ministral-3 via `langchain-ollama` | — |
| Sentiment (CrewAI) | Ollama ministral-3 via litellm | Groq llama-3.1-8b-instant |
| ADK Web (Orchestrator) | Ollama ministral-3 via `openai/` prefix | — |
