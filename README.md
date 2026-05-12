# FinSight — Multi-Agent Investment Research System

An autonomous multi-agent system that answers investment queries like *"Should I invest in NVIDIA given my current portfolio and risk profile?"* by coordinating four specialized agents across different frameworks, communicating via the **Agent-to-Agent (A2A)** protocol, and using **MCP (Model Context Protocol)** servers for external tool access.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    ADK Web UI / API                      │
│                   (port 8001)                            │
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
┌────▼────┐   ┌─────▼──────┐  ┌───────▼────────┐
│ SEC     │   │ yfinance   │  │ Reddit/Twitter  │
│ EDGAR   │   │ MCP :8010  │  │ MCP :8030      │
│ MCP:8020│   │ Python Run │  │ SEC Insider    │
│         │   │ :8040      │  │ :8020          │
└─────────┘   └────────────┘  └────────────────┘
```

## Agents

| Agent | Framework | Role | Port |
|---|---|---|---|
| **Orchestrator** | Google ADK + LiteLLM | Intent parsing, sub-task dispatch, result synthesis | 8001 |
| **RAG** | LlamaIndex + ChromaDB + Groq | SEC filings retrieval, hybrid search (BM25 + dense), citation-backed insights | 8002 |
| **Quant** | LangGraph + yfinance | Sharpe ratio, Beta, VaR, volatility, conditional stress testing vs DCF valuation | 8003 |
| **Sentiment** | CrewAI (4 sub-agents) | Social media sentiment, analyst commentary, insider trading signals | 8004 |

## Tech Stack

| Layer | Technology |
|---|---|
| Agent Communication | Google A2A Protocol (JSON-RPC over HTTP) |
| Tool Access | MCP (Model Context Protocol) |
| Orchestrator | Google ADK (Agent Development Kit) |
| RAG | LlamaIndex + ChromaDB (local) + HuggingFace embeddings |
| Quant | LangChain + LangGraph |
| Sentiment | CrewAI |
| LLM | Groq (via LiteLLM) |
| Embeddings | sentence-transformers (local, all-MiniLM-L6-v2) |
| Storage | ChromaDB (local vector store) |
| Infra | Docker Compose |

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Groq API key (free at https://console.groq.com)

### Setup

```bash
# Clone
git clone https://github.com/Harmanankohli/FinSight.git
cd multi-agent-investment-system

# Create virtualenv & install
uv venv --python 3.12
.venv\Scripts\activate   # Windows
uv pip install -r <(uv pip compile pyproject.toml)

# Set API key
set GROQ_API_KEY=gsk_your_key_here
```

### Run All Services

**Quick start (single command):**
```bash
run_adk_web.bat
```
This starts all agents + MCP servers + ADK Web UI on port 8001.

**Or individually with Docker:**
```bash
docker-compose up --build
```

**Or manually start services:**

```bash
# Terminal 1 — MCP servers
uvicorn mcp_servers.yfinance_server:get_app --port 8010
uvicorn mcp_servers.sec_edgar_server:get_app --port 8020
uvicorn mcp_servers.python_runner_server:get_app --port 8040

# Terminal 2 — Agents
uvicorn agent_2_llamaindex.server:app --port 8002
uvicorn agent_3_langgraph.server:app --port 8003
uvicorn agent_4_crewai.server:app --port 8004

# Terminal 3 — ADK Web UI
adk web --port 8001 agents
```

### Test the System

```bash
# Via the gateway API
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{"query":"Should I invest in NVDA?","portfolio":["AAPL","MSFT"],"risk_profile":"moderate"}'
```

Or open http://localhost:8001 in your browser for the ADK Web UI.

## Project Structure

```
├── agent_1_adk/              # ADK Orchestrator
│   ├── gateway.py            # REST API gateway
│   ├── a2a_client.py         # A2A protocol client
│   ├── intent_parser.py      # Query → QueryContext parsing
│   ├── report_generator.py   # Synthesizes InvestmentBrief
│   └── orchestrator.py       # ADK Agent definition
│
├── agent_2_llamaindex/       # RAG Agent
│   ├── server.py             # A2A server (Starlette)
│   ├── index_manager.py      # Multi-index router (SEC/earnings/news)
│   ├── hybrid_search.py      # BM25 + dense + RRF + reranker
│   └── document_ingestion.py # MCP ingestion pipeline
│
├── agent_3_langgraph/        # Quant Agent
│   ├── server.py             # A2A server
│   ├── graph.py              # LangGraph state machine
│   ├── nodes.py              # Price fetch, metrics, stress test, DCF
│   └── state.py              # QuantAnalysisState schema
│
├── agent_4_crewai/           # Sentiment Agent
│   ├── server.py             # A2A server
│   ├── crew.py               # 4-agent CrewAI definition
│   └── mcp_tools.py          # MCP tool wrappers
│
├── agents/finsight_agent/    # ADK Web-compatible agent
│
├── mcp_servers/              # Custom MCP Servers
│   ├── yfinance_server.py    # Stock prices & financials
│   ├── sec_edgar_server.py   # SEC EDGAR API
│   ├── reddit_server.py      # Reddit sentiment
│   └── python_runner_server.py # Sandboxed Python executor
│
├── shared/                   # Shared libraries
│   ├── models.py             # Pydantic data models
│   ├── a2a_schema.py         # A2A protocol types (re-exports a2a-sdk)
│   └── mcp_client.py         # MCP client wrapper with retry
│
├── tests/                    # Pytest suite (8 tests)
├── docker-compose.yml
└── pyproject.toml
```

## Testing

```bash
.venv\Scripts\activate
python -m pytest tests/ -v
```

Tests cover: A2A communication, MCP client retry, LangGraph state branching, RRF merge, document ingestion, CrewAI crew building.

## License

Apache 2.0
