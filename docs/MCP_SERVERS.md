# MCP Server

FinSight uses a single unified MCP server (`mcp_servers/finsight_server.py`) that hosts both the **agent registry** and all **data tools**. This follows the a2a_mcp reference pattern from a2aproject/a2a-samples.

## Overview

| Port | Tools | Registry |
|---|---|---|
| 8010 | `get_prices`, `get_financials`, `get_options_chain`, `get_company_filings`, `full_text_search`, `get_news_sentiment`, `get_earnings_calendar`, `execute_python` | `find_agent`, `resource://agent_cards/list`, `resource://agent_cards/{name}` |

No API keys required (SEC uses public API, news uses RSS feeds).

## Running

```bash
cd multi-agent-investment-system
.venv\Scripts\activate
python -m uvicorn mcp_servers.finsight_server:get_app --host 0.0.0.0 --port 8010 --log-level info
```

## Agent Registry

The server loads agent cards from `agent_cards/*.json`, generates embeddings via `sentence-transformers`, and exposes:

| Tool / Resource | Description |
|---|---|
| `find_agent(query)` | Semantic search — finds the best agent for a natural language task |
| `resource://agent_cards/list` | Lists all available agent card URIs |
| `resource://agent_cards/{name}` | Retrieves a specific agent card by name (e.g., `rag_agent`) |

## Tools

### yfinance Tools

Fetches stock data via the `yfinance` Python package.

| Tool | Parameters | Returns |
|---|---|---|
| `get_prices` | `ticker` (str), `period` (str, default "1y"), `interval` (str, default "1d") | OHLCV data array |
| `get_financials` | `ticker` (str) | income_statement, balance_sheet, cash_flow, info |
| `get_options_chain` | `ticker` (str), `expiration` (str, optional) | Option expiration dates or calls/puts |

### SEC EDGAR Tools

Fetches SEC filings from `data.sec.gov`. Uses public API — no key required. Rate limits apply (10 req/sec).

| Tool | Parameters | Returns |
|---|---|---|
| `get_company_filings` | `ticker` (str), `form_types` (str, comma-separated, optional), `limit` (int, default 10) | Filing list with form, date, EDGAR URL |
| `full_text_search` | `query` (str), `ticker` (str, optional) | Relevance-scored search results |

**Important**: Requires a valid `User-Agent` header. The SEC blocks generic user agents. Our server uses `"FinSight Research (contact@finsight.com)"`.

### Financial News Tools

Aggregates news from free RSS feeds: Yahoo Finance, CNBC, MarketWatch, Seeking Alpha. Computes VADER sentiment scores.

| Tool | Parameters | Returns |
|---|---|---|
| `get_news_sentiment` | `ticker` (str), `limit` (int, default 10) | Articles with sentiment scores, aggregates |
| `get_earnings_calendar` | `ticker` (str) | Earnings date status |

The server dynamically resolves company names via the SEC ticker file for better article matching.

### Python Runner Tool

Sandboxed Python execution for ad-hoc analysis. AST-based import restriction prevents unsafe imports.

| Tool | Parameters | Returns |
|---|---|---|
| `execute_python` | `code` (str), `timeout` (int, default 30) | success, stdout, stderr, result |

**Restricted imports**: `os`, `subprocess`, `shutil`, `socket`, `ctypes`, `importlib`, `pickle`, `inspect`, `sys`

**Available libraries**: pandas (`pd`), numpy (`np`), math, json, datetime, random, statistics, itertools, collections, functools, typing

## Adding a New Tool

1. Add a new function with `@app.tool()` decorator in `mcp_servers/finsight_server.py`
2. The tool is automatically discovered by any agent that connects and calls `list_tools()`

```python
@app.tool()
async def my_tool(param: str) -> dict:
    """Tool description with Args and Returns."""
    return {"result": param}
```

## MCP Client Usage

Agents connect via SSE and call tools by name (routed automatically):

```python
from shared.mcp_client import MCPClient

mcp = MCPClient(configs=[MCPServerConfig(name="finsight", url="http://localhost:8010/sse")])
await mcp.connect_all()
result = await mcp.call_tool_by_name("get_prices", {"ticker": "NVDA"})
```
