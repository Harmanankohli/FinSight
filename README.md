# FinSight — Multi-Agent Investment Research System

An autonomous multi-agent system that answers investment queries like *"Should I invest in NVIDIA given my current portfolio and risk profile?"* by coordinating four specialized agents across different frameworks, communicating via the **Agent-to-Agent (A2A)** protocol, and using **MCP (Model Context Protocol)** servers for external tool access.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│              ADK Web UI / API (port 8001)                │
│           Google ADK Orchestrator Agent                  │
│       Dynamically discovers agents via A2A cards         │
└────────────────────┬─────────────────────────────────────┘
                     │ A2A Protocol (JSON-RPC over HTTP)
     ┌───────────────┼───────────────────┐
     ▼               ▼                   ▼
┌──────────┐  ┌────────────┐  ┌────────────────┐
│ Agent 2  │  │  Agent 3   │  │    Agent 4      │
│LlamaIndex│  │ LangGraph  │  │    CrewAI       │
│ RAG      │  │ Quant      │  │ Sentiment       │
│ :8002    │  │ :8003      │  │ :8004           │
└────┬─────┘  └─────┬──────┘  └───────┬─────────┘
     │               │                 │
     ▼               ▼                 ▼
┌────────┐   ┌───────────┐   ┌────────────┐
│  SEC   │   │ yfinance  │   │  Financial │
│ EDGAR  │   │ MCP :8010 │   │  News MCP  │
│:8020   │   │ Python Run│   │  :8025     │
│        │   │ :8040     │   │            │
└────────┘   └───────────┘   └────────────┘
```

## Agents

| Agent | Framework | Role | LLM | Port |
|---|---|---|---|---|
| **Orchestrator** | Google ADK | Intent parsing, sub-task dispatch, result synthesis | Ollama (llama3.2 local) | 8001 |
| **RAG** | LlamaIndex + ChromaDB | SEC filings retrieval, hybrid search, citation-backed insights | Ollama (llama3.2 local) | 8002 |
| **Quant** | LangGraph + yfinance | Sharpe ratio, Beta, VaR, volatility, stress tests, DCF | Ollama (llama3.2 local) | 8003 |
| **Sentiment** | CrewAI (2 agents) | Financial news sentiment, SEC insider analysis | Ollama (llama3.2 local) | 8004 |

## Tech Stack

| Layer | Technology |
|---|---|
| Agent Communication | Google A2A Protocol v1.0 (JSON-RPC over HTTP) |
| Tool Access | MCP (Model Context Protocol) via SSE |
| Orchestrator | Google ADK (Agent Development Kit) |
| RAG | LlamaIndex + ChromaDB (local) + HuggingFace embeddings |
| Quant | LangChain + LangGraph (state machine) |
| Sentiment | CrewAI (parallel data collection + synthesis) |
| LLM | Ollama (llama3.2 local) — no API keys needed |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2, local) |
| Vector Store | ChromaDB (local, persisted) |
| All LLMs | Fully local via Ollama |

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- [Ollama](https://ollama.com) with `ollama pull llama3.2`

### Setup

```bash
git clone https://github.com/Harmanankohli/FinSight.git
cd multi-agent-investment-system

# Create virtualenv & install
uv venv --python 3.12
.venv\Scripts\activate
uv pip install -e .
uv pip install llama-index-llms-ollama langchain-ollama

# Pull local LLM
ollama pull llama3.2

# (Optional) For Sentiment agent with faster inference:
# ollama pull lfm2.5-thinking:1.2b

# Configure .env (no API keys needed for Ollama)
echo LLM_MODEL=llama3.2 > .env
```

### Run All Services

```bash
run_adk_web.bat
```

This starts all 4 MCP servers, 3 agents, and the ADK Web UI.

### Test the System

```bash
# Test each agent individually
curl -X POST http://localhost:8003/a2a \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{"jsonrpc":"2.0","method":"SendMessage","id":"1","params":{"message":{"messageId":"1","role":"ROLE_USER","parts":[{"text":"Analyze NVDA"}]},"metadata":{"ticker":"NVDA","period":"1mo"}}}'
```

Open http://127.0.0.1:8001 in your browser for the ADK Web UI.

## Project Structure

```
├── agent_1_adk/              # ADK Orchestrator
│   ├── gateway.py            # REST API gateway (uses SDK client)
│   ├── a2a_client.py         # A2ADiscoverer + custom client
│   ├── intent_parser.py      # Query → QueryContext parsing
│   └── report_generator.py   # Synthesizes InvestmentBrief
│
├── agent_2_llamaindex/       # RAG Agent
│   ├── server.py             # AgentExecutor pattern
│   ├── executor.py           # A2A AgentExecutor (auto-ingests SEC filings)
│   ├── index_manager.py      # ChromaDB multi-index + Ollama LLM
│   ├── hybrid_search.py      # BM25 + dense + RRF + reranker
│   └── document_ingestion.py # MCP ingestion pipeline
│
├── agent_3_langgraph/        # Quant Agent
│   ├── server.py             # AgentExecutor pattern
│   ├── executor.py           # A2A AgentExecutor
│   ├── graph.py              # LangGraph state machine
│   ├── nodes.py              # Nodes + Ollama LLM summary
│   └── state.py              # QuantAnalysisState schema
│
├── agent_4_crewai/           # Sentiment Agent
│   ├── server.py             # AgentExecutor pattern
│   ├── executor.py           # Parallel MCP data collection
│   ├── crew.py               # 2-agent CrewAI (analysis + synthesis)
│   └── mcp_tools.py          # DynamicMCPTool with Pydantic args_schema
│
├── agents/finsight_agent/    # ADK Web-compatible agent
│
├── mcp_servers/              # Custom MCP Servers
│   ├── yfinance_server.py    # Stock prices & financials
│   ├── sec_edgar_server.py   # SEC EDGAR filings
│   ├── financial_news_server.py  # RSS news + VADER sentiment
│   └── python_runner_server.py   # Sandboxed Python executor
│
├── shared/                   # Shared libraries
│   ├── config.py             # Centralized .env configuration
│   ├── models.py             # Pydantic data models
│   └── mcp_client.py         # MCP client with dynamic tool discovery
│
└── docker-compose.yml
```

## Sample Output

```json
{
  "ticker": "NVDA",
  "final_recommendation": "SELL",
  "confidence_score": 0.485,
  "quant_metrics": {
    "sharpe_ratio": 1.337,
    "annual_volatility": 0.516,
    "beta": 2.151,
    "quant_signal": "SELL"
  },
  "rag_insights": {
    "sources": ["NVDA_10-K_2026-02-25.html"],
    "confidence_score": 0.454
  },
  "sentiment_intelligence": {
    "overall_signal": "neutral",
    "confidence_score": 0.5
  }
}
```

## License

Apache 2.0
