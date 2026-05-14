# Agents Reference

## Agent 1: Orchestrator (ADK)

| Property | Value |
|---|---|
| Framework | Google ADK |
| Port | 8001 |
| Agent Card | `GET /.well-known/agent-card.json` |
| A2A Endpoint | `POST /a2a` |

### Skill

| ID | Name | Description |
|---|---|---|
| `investment_research` | Investment Research | Answer investment queries with a complete research brief |

### Tools (ADK-registered, available to the LLM)

| Tool | Calls | Description |
|---|---|---|
| `query_rag(ticker)` | RAG Agent (port 8002) | Retrieves SEC filings and financial documents |
| `query_quant(ticker)` | Quant Agent (port 8003) | Computes risk metrics and valuations |
| `query_sentiment(ticker)` | Sentiment Agent (port 8004) | Gathers market sentiment and insider signals |

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
| LLM | Ollama (llama3.2) via `llama-index-llms-ollama` |
| Vector Store | ChromaDB (local, persisted to `./chroma_db`) |
| Embeddings | HuggingFace `all-MiniLM-L6-v2` (local) |
| Port | 8002 |
| Agent Card | `GET http://localhost:8002/.well-known/agent-card.json` |
| A2A Endpoint | `POST http://localhost:8002/a2a` |

### Skills

| ID | Name | Description |
|---|---|---|
| `sec_filing_retrieval` | SEC Filing Retrieval | Retrieves and analyzes SEC 10-K, 10-Q, 8-K filings |
| `earnings_summary` | Earnings Summary | Summarizes earnings call transcripts |

### MCP Tools Used

| Server | Tool | Purpose |
|---|---|---|
| SEC EDGAR (`:8020`) | `get_company_filings` | Auto-ingest filings before query |

### Architecture

```
Request → DefaultRequestHandler → RAGAgentExecutor.execute()
  → RAGAgent._ensure_ingested(ticker)
    ├── MCPClient.connect_all()
    └── MCPClient.call_tool_by_name("get_company_filings", {...})
  → FinancialIndexManager.query(ticker, query)
    ├── Try: RouterQueryEngine → Ollama LLM → response
    └── Fallback: SEC filings index directly
  → Response: {summary, sources, relevance_scores, confidence_score}
```

### Index Structure

Four ChromaDB collections:

| Collection | Document Type | Source |
|---|---|---|
| `sec_filings` | 10-K, 10-Q, 8-K | SEC EDGAR MCP |
| `earnings` | Earnings call transcripts | SEC EDGAR MCP |
| `news` | Financial news articles | Financial News MCP |
| `analyst_reports` | Analyst reports | External |

### Sample Response

```json
{
  "ticker": "NVDA",
  "summary": "Key risks for NVDA include market competition and regulatory changes...",
  "sources": ["NVDA_10-K_2026-02-25.html"],
  "relevance_scores": [0.513, 0.492],
  "confidence_score": 0.513
}
```

---

## Agent 3: Quant (LangGraph)

| Property | Value |
|---|---|
| Framework | LangChain + LangGraph |
| LLM | Ollama (llama3.2) via `langchain-ollama` (for summary node only) |
| Data Source | yfinance (direct, not via MCP) |
| Port | 8003 |
| Agent Card | `GET http://localhost:8003/.well-known/agent-card.json` |
| A2A Endpoint | `POST http://localhost:8003/a2a` |

### Skills

| ID | Name | Description |
|---|---|---|
| `quant_analysis` | Quantitative Analysis | Compute Sharpe ratio, Beta, VaR, volatility, DCF valuation, stress tests |

### State Machine

```
fetch_prices → compute_metrics → conditional branch
  ├── high_volatility (annual_vol > 35%) → stress_test
  └── low_volatility → dcf_valuation
  → portfolio_correlation → format_output → llm_summary → END
```

### Nodes

| Node | What it does | Libraries |
|---|---|---|
| `fetch_prices` | Downloads OHLCV from Yahoo Finance | yfinance, pandas |
| `compute_metrics` | Sharpe, Beta, VaR, Volatility, Max Drawdown | numpy, scipy |
| `stress_test` | 4 crash scenarios (2008, Covid, dot-com, recession) | numpy |
| `dcf_valuation` | Discounted cash flow from financial statements | yfinance |
| `portfolio_correlation` | Correlation matrix vs holdings | yfinance, pandas |
| `format_output` | Signal generation + confidence scoring | — |
| `llm_summary` | Natural language summary via Ollama | langchain-ollama |

### Signal Rules

| Condition | Signal |
|---|---|
| Sharpe >= 1.0 | `positive_risk_adjusted_return` |
| Sharpe < 0 | `negative_risk_adjusted_return` |
| Volatility > 35% | `high_volatility` |
| Volatility < 15% | `low_volatility` |
| DCF upside > 20% | `undervalued_dcf` |
| DCF upside < -20% | `overvalued_dcf` |
| CVaR < -5% | `tail_risk` |

Recommendation = BUY if positive signals > negative signals, SELL if negative > positive, else HOLD.

### Sample Response

```json
{
  "ticker": "NVDA",
  "recommendation": "SELL",
  "reasoning": "Based on the analysis, NVDA has a Sharpe ratio of 1.35 indicating...",
  "metrics": {
    "sharpe_ratio": 1.35,
    "annual_volatility": 0.516,
    "beta": 2.155,
    "quant_signal": "SELL",
    "quant_confidence": 0.5,
    "signals": ["positive_risk_adjusted_return", "high_volatility", "tail_risk"]
  },
  "stress_test": {
    "scenarios": {
      "market_crash_2008": {"decline_pct": -0.37},
      "covid_crash_2020": {"decline_pct": -0.34}
    }
  },
  "dcf_valuation": null
}
```

---

## Agent 4: Sentiment (CrewAI)

| Property | Value |
|---|---|
| Framework | CrewAI |
| LLM | Ollama (llama3.2) via litellm |
| Data Collection | Parallel via `asyncio.gather` |
| Port | 8004 |
| Agent Card | `GET http://localhost:8004/.well-known/agent-card.json` |
| A2A Endpoint | `POST http://localhost:8004/a2a` |

### Skills

| ID | Name | Description |
|---|---|---|
| `sentiment_analysis` | Sentiment & Narrative Intelligence | Analyze news sentiment, SEC filings, produce investment narrative |

### MCP Tools Used

| Server | Tool | Purpose |
|---|---|---|
| Financial News (`:8025`) | `get_news_sentiment` | RSS news with VADER sentiment |
| SEC EDGAR (`:8020`) | `get_company_filings` | SEC filings for insider context |

### Architecture

```
Request → SentimentAgentExecutor.execute()
  → SentimentAgent.analyze(ticker)
    ├── _connect() → MCPClient.connect_all()
    ├── _collect_data_parallel(ticker)
    │   ├── call("get_news_sentiment", {ticker})  ─┐
    │   ├── call("get_company_filings", {ticker})  ─┤─ asyncio.gather
    │   └── results merged                          ─┘
    └── SentimentIntelligenceCrew.analyze(ticker, precollected_data)
        ├── Analysis Agent     # Extracts sentiment signals
        └── Synthesis Agent    # Writes 2-3 paragraph narrative
  → Response: {overall_signal, narrative, confidence_score}
```

### Sample Response

```json
{
  "overall_signal": "neutral",
  "confidence_score": 0.64,
  "narrative": "NVIDIA Corporation (NVDA) is poised to navigate its corporate landscape...",
  "key_risks": ["Activist investor pressure", "Market volatility"],
  "key_catalysts": ["Corporate governance improvements", "AI chip demand"]
}
```
