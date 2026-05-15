# Agents Reference

## Agent 1: Orchestrator (ADK)

| Property | Value |
|---|---|---|
| Framework | Google ADK |
| Port | 8001 |
| Agent Card | `agent_1_adk/agent_card.json` *(only used when running as standalone A2A server via `main.py`)* |
| Discovery | Seed URLs (`AGENT_SEED_URLS`) with 3x retry |

The orchestrator is an ADK `LlmAgent` that:

1. **Discovers agents at module load** via `SubAgentClient.discover_sync()` — sync HTTP with retries
2. **Generates one ADK tool per discovered agent** — dynamically, no hardcoded tool definitions
3. **Delegates tasks via A2A** — each tool sends a `SendMessageRequest` to the remote agent
4. **Synthesizes results** — LLM collects all agent responses and produces a recommendation

A2A clients for sub-agent communication are created lazily on first tool call via `create_client()`, ensuring correct event loop context.

### Architecture

```
Module load → SubAgentClient.discover_sync()
  ├── GET http://localhost:8002/.well-known/agent-card.json
  ├── GET http://localhost:8003/.well-known/agent-card.json
  └── GET http://localhost:8004/.well-known/agent-card.json
  → _agent_list populated → _tools created (1 per agent)

Tool call (e.g. financial_rag_agent("NVDA"))
  → _get_a2a_client(agent_name)  # lazy, cached
  → create_client(url)           # A2A SDK
  → client.send_message(req)     # JSON-RPC to sub-agent
  → _extract_text(task)          # handles text + data responses
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
| LLM | Ollama (qwen2.5:7b) via `llama-index-llms-ollama` |
| Vector Store | ChromaDB (local, persisted to `./chroma_db`) |
| Embeddings | HuggingFace `all-MiniLM-L6-v2` (local) |
| Port | 8002 |
| Agent Card | `agent_cards/rag_agent.json` |
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
      ├── Try: RouterQueryEngine → Ollama LLM → response
      └── Fallback: SEC filings index directly
  → Yields: {response_type: "data", content: result, is_task_complete: true}
```

---

## Agent 3: Quant (LangGraph)

| Property | Value |
|---|---|
| Framework | LangChain + LangGraph |
| Executor | `GenericAgentExecutor(QuantAgent)` |
| LLM | Ollama (qwen2.5:7b) via `langchain-ollama` (summary node only) |
| Data Source | yfinance (direct) |
| Port | 8003 |
| Agent Card | `agent_cards/quant_agent.json` |
| A2A Endpoint | `POST /a2a` |

### Skills

| ID | Name | Description |
|---|---|---|
| `quant_analysis` | Quantitative Analysis | Compute Sharpe ratio, Beta, VaR, volatility, DCF valuation, stress tests |

### Architecture

```
Request → DefaultRequestHandler → GenericAgentExecutor(QuantAgent)
  → QuantAgent.stream(query, context_id, task_id)
    → QuantAgent.analyze(ticker)
      → fetch_prices → compute_metrics → conditional branch
        ├── high_volatility (annual_vol > 35%) → stress_test
        └── low_volatility → dcf_valuation
      → portfolio_correlation → format_output → llm_summary
  → Yields: {response_type: "data", content: result, is_task_complete: true}
```

---

## Agent 4: Sentiment (CrewAI)

| Property | Value |
|---|---|
| Framework | CrewAI |
| Executor | `GenericAgentExecutor(SentimentAgent)` |
| LLM | Ollama (qwen2.5:7b) via litellm |
| Data Collection | Parallel via `asyncio.gather` |
| Port | 8004 |
| Agent Card | `agent_cards/sentiment_agent.json` |
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
