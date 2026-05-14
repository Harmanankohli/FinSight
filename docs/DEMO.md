# Demo: End-to-End Investment Query

This walkthrough shows the full pipeline for the query *"Should I invest in NVDA?"*

## Architecture Flow

```
User Query → ADK Web (8001) → ADK Agent (LLM decides)
  → query_rag → RAG Agent (8002) → finsight-mcp (8010: SEC EDGAR) → ChromaDB → Ollama LLM
  → query_quant → Quant Agent (8003) → finsight-mcp (8010: yfinance) → LangGraph → Ollama LLM
  → query_sentiment → Sentiment Agent (8004) → finsight-mcp (8010: News + SEC) → CrewAI
Orchestrator synthesizes → Final Investment Brief
```

All MCP tools are served from a single `finsight-mcp` server (port 8010).

## Step 1: Send Query

Open http://127.0.0.1:8001 in a browser and type: *"Should I invest in NVDA?"*

Or via curl:
```bash
curl -X POST http://localhost:8002/a2a \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{"jsonrpc":"2.0","method":"SendMessage","id":"1","params":{"message":{"messageId":"1","role":"ROLE_USER","parts":[{"text":"Research NVDA"}]},"metadata":{"ticker":"NVDA"}}}'
```

## Step 2: Orchestrator Dispatches to Agents

The ADK Web agent's LLM (Ollama qwen3.5) calls all three tools in parallel:

### Tool Call 1: `query_rag("NVDA")` → RAG Agent (port 8002)

```
A2A Request → GenericAgentExecutor(RAGAgent)
  → RAGAgent.stream()
    → _ensure_ingested() → finsight-mcp (get_company_filings)
    → FinancialIndexManager.query() → ChromaDB + Ollama
  → Response: {summary, sources, confidence_score}
```

### Tool Call 2: `query_quant("NVDA")` → Quant Agent (port 8003)

```
A2A Request → GenericAgentExecutor(QuantAgent)
  → QuantAgent.stream()
    → fetch_prices (finsight-mcp / yfinance)
    → compute_metrics (Sharpe, Beta, VaR, Volatility)
    → conditional branch: high vol → stress_test | low vol → DCF
    → portfolio_correlation → format_output → Ollama summary
  → Response: {recommendation, metrics, stress_test, dcf_valuation}
```

### Tool Call 3: `query_sentiment("NVDA")` → Sentiment Agent (port 8004)

```
A2A Request → GenericAgentExecutor(SentimentAgent)
  → SentimentAgent.stream()
    → Parallel MCP data (finsight-mcp):
      ├── get_news_sentiment (RSS + VADER)
      └── get_company_filings (SEC EDGAR)
    → 2-agent CrewAI (analysis + synthesis)
  → Response: {overall_signal, narrative, confidence_score}
```

## Step 3: Orchestrator Synthesizes

The ADK Web agent's LLM receives all three results and generates a final BUY/HOLD/SELL recommendation with rationale.

## Sample Full Response

```json
{
  "ticker": "NVDA",
  "final_recommendation": "SELL",
  "confidence_score": 0.485,
  "quant_metrics": {
    "sharpe_ratio": 1.34,
    "annual_volatility": 0.52,
    "beta": 2.15,
    "quant_signal": "SELL"
  },
  "rag_insights": {
    "forward_guidance": "Management expects data center segment to grow 30% YoY",
    "key_risks": ["Export restrictions", "Competition from AMD"],
    "confidence_score": 0.51
  },
  "sentiment_intelligence": {
    "overall_signal": "neutral",
    "confidence_score": 0.5,
    "key_risks": ["Activist investor pressure"],
    "key_catalysts": ["AI chip demand growth"]
  }
}
```

## Running the Demo

```bash
# Terminal 1 — Unified MCP Server (port 8010)
python -m uvicorn mcp_servers.finsight_server:get_app --host 0.0.0.0 --port 8010

# Terminal 2 — RAG Agent (port 8002)
python -m uvicorn agent_2_llamaindex.server:app --host 0.0.0.0 --port 8002

# Terminal 3 — Quant Agent (port 8003)
python -m uvicorn agent_3_langgraph.server:app --host 0.0.0.0 --port 8003

# Terminal 4 — Sentiment Agent (port 8004)
python -m uvicorn agent_4_crewai.server:app --host 0.0.0.0 --port 8004

# Terminal 5 — ADK Web UI (port 8001)
adk web --port 8001 agents
```

## Direct A2A Testing

Test individual agents without the ADK Web UI:

```bash
# Test RAG agent
curl -X POST http://localhost:8002/a2a \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{"jsonrpc":"2.0","method":"SendMessage","id":"1","params":{"message":{"messageId":"1","role":"ROLE_USER","parts":[{"text":"Research NVDA"}]},"metadata":{"ticker":"NVDA"}}}'

# Test Quant agent
curl -X POST http://localhost:8003/a2a \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{"jsonrpc":"2.0","method":"SendMessage","id":"1","params":{"message":{"messageId":"1","role":"ROLE_USER","parts":[{"text":"Analyze NVDA"}]},"metadata":{"ticker":"NVDA","period":"5y"}}}'

# Test Sentiment agent
curl -X POST http://localhost:8004/a2a \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{"jsonrpc":"2.0","method":"SendMessage","id":"1","params":{"message":{"messageId":"1","role":"ROLE_USER","parts":[{"text":"Sentiment for NVDA"}]},"metadata":{"ticker":"NVDA"}}}'
```
