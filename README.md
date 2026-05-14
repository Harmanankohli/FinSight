# FinSight — Multi-Agent Investment Research System

An autonomous multi-agent system that answers investment queries like *"Should I invest in NVIDIA given my current portfolio and risk profile?"* by coordinating four specialized agents across different frameworks, communicating via the **Agent-to-Agent (A2A)** protocol, and using **MCP (Model Context Protocol)** servers for external tool access.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│              ADK Web UI / API (port 8001)                    │
│           Orchestrator (gateway.py + planner)                │
│         Decomposes query → discovers agents → executes       │
└──────────────────────┬───────────────────────────────────────┘
                       │ A2A Protocol (JSON-RPC over HTTP)
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  Agent Pool                                                   │
│  RAG (:8002)    Quant (:8003)    Sentiment (:8004)           │
│  (LlamaIndex)   (LangGraph)      (CrewAI)                    │
└────────┬────────────┬────────────────┬───────────────────────┘
         │            │                │
         ▼            ▼                ▼
┌──────────────────────────────────────────────────────────────┐
│          Unified finsight-mcp Server (port 8010)              │
│  ┌─────────────────┐  ┌──────────────────────────────────┐   │
│  │ Agent Registry  │  │  Data Sources                    │   │
│  │ find_agent()    │  │  get_prices, get_financials,     │   │
│  │ resource://cards│  │  get_company_filings,            │   │
│  └─────────────────┘  │  full_text_search,              │   │
│                        │  get_news_sentiment,            │   │
│                        │  execute_python, ...            │   │
│                        └──────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

## Agents

| Agent | Framework | Port | Agent Card | Executor |
|---|---|---|---|---|
| **Orchestrator** | Google ADK | 8001 | `agent_cards/orchestrator_agent.json` | `gateway.py` + `A2AClient` |
| **RAG** | LlamaIndex + ChromaDB | 8002 | `agent_cards/rag_agent.json` | `GenericAgentExecutor(RAGAgent)` |
| **Quant** | LangGraph + yfinance | 8003 | `agent_cards/quant_agent.json` | `GenericAgentExecutor(QuantAgent)` |
| **Sentiment** | CrewAI | 8004 | `agent_cards/sentiment_agent.json` | `GenericAgentExecutor(SentimentAgent)` |
| **MCP Server** | FastMCP | 8010 | `mcp_servers/finsight_server.py` | Registry + all data tools |

## Tech Stack

| Layer | Technology |
|---|---|
| Agent Communication | Google A2A Protocol v1.0 (JSON-RPC over HTTP) |
| Agent Discovery | MCP Protocol (agent registry with embedding search) |
| Agent Cards | Declarative JSON files (`agent_cards/*.json`) |
| Shared Executor | `GenericAgentExecutor` + `BaseAgent` pattern |
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
uv pip install llama-index-llms-ollama langchain-ollama sentence-transformers

# Pull local LLM
ollama pull llama3.2

# (Optional) For Sentiment agent with faster inference:
# ollama pull lfm2.5-thinking:1.2b

# Copy configuration template
copy .env.example .env
# Edit .env if needed — defaults work for local Ollama
```

### Run All Services

Each service runs in its own terminal window:

```bash
# Terminal 1: Unified MCP Server (agent registry + all data tools)
uv run python -m uvicorn mcp_servers.finsight_server:get_app --host 0.0.0.0 --port 8010

# Terminal 2: RAG Agent
uv run python -m uvicorn agent_2_llamaindex.server:app --host 0.0.0.0 --port 8002

# Terminal 3: Quant Agent
uv run python -m uvicorn agent_3_langgraph.server:app --host 0.0.0.0 --port 8003

# Terminal 4: Sentiment Agent
uv run python -m uvicorn agent_4_crewai.server:app --host 0.0.0.0 --port 8004

# Terminal 5: ADK Web UI
.venv\Scripts\activate && adk web --port 8001 agents
```

**Startup order:**
1. Unified MCP Server (`localhost:8010`) — agent registry + all data tools
2. Sub-agents — RAG (`:8002`), Quant (`:8003`), Sentiment (`:8004`)
3. ADK Web UI (`:8001`) — serves the agent playground at http://127.0.0.1:8001

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
│   ├── gateway.py            # REST API gateway (workflow orchestrator)
│   ├── orchestrator.py       # ADK-native orchestrator alternative
│   ├── a2a_client.py         # A2ADiscoverer + A2AClient (unified)
│   ├── planner.py            # Query decomposition → ordered TaskList
│   └── report_generator.py   # Synthesizes InvestmentBrief
│
├── agent_2_llamaindex/       # RAG Agent
│   ├── server.py             # GenericAgentExecutor(RAGAgent)
│   ├── executor.py           # RAGAgent extends BaseAgent with stream()
│   ├── index_manager.py      # ChromaDB multi-index + Ollama LLM
│   ├── hybrid_search.py      # BM25 + dense + RRF + reranker
│   └── document_ingestion.py # MCP ingestion pipeline
│
├── agent_3_langgraph/        # Quant Agent
│   ├── server.py             # GenericAgentExecutor(QuantAgent)
│   ├── executor.py           # QuantAgent extends BaseAgent with stream()
│   ├── graph.py              # LangGraph state machine
│   ├── nodes.py              # Nodes + Ollama LLM summary
│   └── state.py              # QuantAnalysisState schema
│
├── agent_4_crewai/           # Sentiment Agent
│   ├── server.py             # GenericAgentExecutor(SentimentAgent)
│   ├── executor.py           # SentimentAgent extends BaseAgent with stream()
│   ├── crew.py               # 2-agent CrewAI (analysis + synthesis)
│   └── mcp_tools.py          # DynamicMCPTool with Pydantic args_schema
│
├── agents/finsight_agent/    # ADK Web-compatible agent
│
├── agent_cards/              # Declarative A2A Agent Card JSON files
│   ├── orchestrator_agent.json
│   ├── rag_agent.json
│   ├── quant_agent.json
│   └── sentiment_agent.json
│
├── mcp_servers/              # Single unified MCP Server
│   ├── finsight_server.py    # Registry + all data tools (port 8010)
│   └── Dockerfile
│
├── shared/                   # Shared libraries
│   ├── base_agent.py         # BaseAgent abstract class
│   ├── generic_executor.py   # GenericAgentExecutor
│   ├── workflow.py           # WorkflowGraph state machine
│   ├── types.py              # Shared Pydantic models
│   ├── config.py             # Centralized .env configuration
│   ├── models.py             # Pydantic data models
│   └── mcp_client.py         # MCP client with dynamic tool discovery
│
├── tests/                    # Test suite
│   ├── test_base_agent.py
│   ├── test_planner.py
│   ├── test_workflow.py
│   ├── test_agent_cards.py
│   ├── test_a2a_communication.py
│   ├── test_quant_graph.py
│   ├── test_rag_pipeline.py
│   └── test_sentiment_crew.py
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
