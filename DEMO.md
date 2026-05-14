# Demo: End-to-End Investment Query

This walkthrough shows the full pipeline for the query *"Should I invest in NVDA?"*

## Architecture Flow

```
User Query → ADK Web (8001) → Orchestrator Agent (LLM decides)
  → query_rag → RAG Agent (8002) → SEC EDGAR MCP (8020) → ChromaDB → Ollama LLM
  → query_quant → Quant Agent (8003) → yfinance → LangGraph → Ollama LLM summary
  → query_sentiment → Sentiment Agent (8004) → Financial News MCP (8025) → CrewAI
Orchestrator synthesizes → Final Investment Brief
```

## Step 1: Send Query

```bash
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{"query":"Should I invest in NVDA?","portfolio":["AAPL","MSFT"],"risk_profile":"moderate"}'
```

Or open http://127.0.0.1:8001 in a browser and type: *"Should I invest in NVDA?"*

## Step 2: Orchestrator Dispatches to Agents

The ADK Web agent's LLM (Ollama llama3.2) decides to call all three tools:

### Tool Call 1: `query_rag("NVDA")` → RAG Agent

Sends A2A request to port 8002:
```json
{
  "method": "SendMessage",
  "params": {
    "message": {"role": "ROLE_USER", "parts": [{"text": "Research NVDA"}]},
    "metadata": {"ticker": "NVDA", "period": "5y"}
  }
}
```

The RAG executor:
1. Auto-ingests SEC 10-K filings from SEC EDGAR MCP (port 8020)
2. Stores them in ChromaDB vector store
3. Queries ChromaDB with Ollama via LlamaIndex
4. Returns structured response:

```json
{
  "ticker": "NVDA",
  "summary": "Key risks for NVDA include market and economic risks...",
  "sources": ["NVDA_10-K_2026-02-25.html"],
  "confidence_score": 0.513
}
```

### Tool Call 2: `query_quant("NVDA")` → Quant Agent

Sends A2A request to port 8003:

The LangGraph state machine runs 6 nodes:
1. `fetch_prices` — Downloads NVDA price history via yfinance
2. `compute_metrics` — Calculates Sharpe, Beta, VaR, Volatility
3. High volatility detected (annual volatility > 35%) → routes to `stress_test`
4. `stress_test` — Simulates 4 crash scenarios (2008-style -37%, Covid -34%, etc.)
5. `portfolio_correlation` — Computes correlation with AAPL, MSFT
6. `format_output` + `llm_summary` — Ollama generates natural language summary

```json
{
  "recommendation": "SELL",
  "reasoning": "Sharpe: 1.34, Vol: 0.52, Beta: 2.15 | Stress test CVaR: -0.067",
  "metrics": {
    "sharpe_ratio": 1.34,
    "annual_volatility": 0.52,
    "beta": 2.15,
    "var_95_daily": -0.048,
    "max_drawdown": -0.66
  },
  "stress_test": {
    "dot_com_bubble": {"decline_pct": -0.49, "projected_price": 115.90},
    "covid_crash_2020": {"decline_pct": -0.34, "projected_price": 149.98},
    "market_crash_2008": {"decline_pct": -0.37, "projected_price": 143.17},
    "mild_recession": {"decline_pct": -0.15, "projected_price": 193.16}
  }
}
```

### Tool Call 3: `query_sentiment("NVDA")` → Sentiment Agent

Sends A2A request to port 8004. The executor:
1. Calls MCP tools in parallel via `asyncio.gather`:
   - `get_news_sentiment` (financial-news MCP, port 8025) — fetches RSS news, computes VADER sentiment
   - `get_company_filings` (SEC EDGAR MCP, port 8020) — fetches Form 4 insider filings
2. Passes collected data to 2-agent CrewAI (analysis + synthesis)
3. CrewAI agents use Ollama to analyze and write narrative

```json
{
  "overall_signal": "neutral",
  "confidence_score": 0.5,
  "narrative": "NVIDIA Corporation (NVDA) is currently presenting a neutral outlook...",
  "key_risks": ["Activist investor pressure", "Market volatility"],
  "key_catalysts": ["Corporate governance improvements"]
}
```

## Step 3: Orchestrator Synthesizes

The ADK Web agent's LLM receives all three results and generates the final response:

```
Based on my analysis of NVDA:

**Quantitative Analysis (SELL signal):**
The stock shows a Sharpe ratio of 1.34 but high annual volatility of 0.52 with a beta of 2.15. 
Stress tests show potential losses of 15-49% in various crash scenarios. Max drawdown of -66% 
indicates significant downside risk.

**Fundamental Analysis:**
SEC filings indicate a company with strong growth in its data center segment and 
continued leadership in AI chips.

**Market Sentiment: (neutral)**
Analyst consensus is neutral with moderate confidence.

**Overall Recommendation: SELL**
Given the high volatility, elevated beta, and significant drawdown risks relative to 
the neutral sentiment, NVDA appears overvalued at current levels.
```

## Sample Full Response

```json
{
  "ticker": "NVDA",
  "generated_at": "2026-05-12T13:44:45Z",
  "final_recommendation": "SELL",
  "confidence_score": 0.485,
  "recommendation_rationale": "Quant: Sharpe=1.34, Vol=0.52, Beta=2.15",
  "quant_metrics": {
    "sharpe_ratio": 1.34,
    "annual_volatility": 0.52,
    "beta": 2.15,
    "dcf_intrinsic_value": null,
    "quant_signal": "SELL",
    "quant_confidence": 0.5
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
# Terminal 1 — Start all services
run_adk_web.bat

# Terminal 2 — Test via API
python tools/test_agents.py

# Or open browser: http://localhost:8001
```

## MCP Server Tests

All tool calls happen without LLM inference:

```bash
python tools/test_all_mcp.py
```

Expected output:
```
SERVER: yfinance (8010)
  TOOL: get_prices → AAPL OHLCV data
  TOOL: get_financials → Income statement
SERVER: sec-edgar (8020)
  TOOL: get_company_filings → SEC filings
SERVER: financial-news (8025)
  TOOL: get_news_sentiment → Articles with sentiment scores
SERVER: python-runner (8040)
  TOOL: execute_python → sum([1,2,3]) = 6
```
