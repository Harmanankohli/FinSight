# MCP Servers

FinSight uses four custom MCP servers, each running as a standalone `FastMCP` SSE server behind uvicorn.

## Overview

| Server | Port | Tools | API Key Required |
|---|---|---|---|
| yfinance | 8010 | `get_prices`, `get_financials`, `get_options_chain` | No |
| SEC EDGAR | 8020 | `get_company_filings`, `full_text_search` | No (public API) |
| Financial News | 8025 | `get_news_sentiment`, `get_earnings_calendar` | No (RSS feeds) |
| Python Runner | 8040 | `execute_python` | No |

All servers use the MCP SSE transport and are served via uvicorn.

## Running

```bash
# Start all MCP servers
cd multi-agent-investment-system
.venv\Scripts\activate

# Individual servers
uvicorn mcp_servers.yfinance_server:get_app --port 8010
uvicorn mcp_servers.sec_edgar_server:get_app --port 8020
uvicorn mcp_servers.financial_news_server:get_app --port 8025
uvicorn mcp_servers.python_runner_server:get_app --port 8040
```

## Testing

```bash
python tools/test_all_mcp.py
```

Expected output:
```
SERVER: yfinance (8010)
  TOOL: get_prices → ticker, period, data[]
  TOOL: get_financials → income_statement, balance_sheet, cash_flow
  TOOL: get_options_chain → expirations[] or calls/puts

SERVER: sec-edgar (8020)
  TOOL: get_company_filings → filings[] with form, date, url
  TOOL: full_text_search → results[] with score, ticker, form

SERVER: financial-news (8025)
  TOOL: get_news_sentiment → articles[] with sentiment
  TOOL: get_earnings_calendar → status

SERVER: python-runner (8040)
  TOOL: execute_python → success, stdout, result
```

## Server Details

### yfinance (port 8010)

Fetches stock data via the `yfinance` Python package. No API key needed.

| Tool | Parameters | Returns |
|---|---|---|
| `get_prices` | `ticker` (str), `period` (str, default "1y"), `interval` (str, default "1d") | OHLCV data array |
| `get_financials` | `ticker` (str) | income_statement, balance_sheet, cash_flow, info |
| `get_options_chain` | `ticker` (str), `expiration` (str, optional) | Option expiration dates or calls/puts |

### SEC EDGAR (port 8020)

Fetches SEC filings from `data.sec.gov`. Uses public API — no key required. Rate limits apply (10 req/sec).

| Tool | Parameters | Returns |
|---|---|---|
| `get_company_filings` | `ticker` (str), `form_types` (str, comma-separated, optional), `limit` (int, default 10) | Filing list with form, date, EDGAR URL |
| `full_text_search` | `query` (str), `ticker` (str, optional) | Relevance-scored search results |

**Important**: Requires a valid `User-Agent` header. The SEC blocks generic user agents. Our servers use `"FinSight Research (contact@finsight.com)"`.

### Financial News (port 8025)

Aggregates news from free RSS feeds: Yahoo Finance, CNBC, MarketWatch, Seeking Alpha. Computes VADER sentiment scores. No API key needed.

| Tool | Parameters | Returns |
|---|---|---|
| `get_news_sentiment` | `ticker` (str), `limit` (int, default 10) | Articles with sentiment scores, aggregates |
| `get_earnings_calendar` | `ticker` (str) | Earnings date status |

The server dynamically resolves company names via the SEC ticker file for better article matching.

### Python Runner (port 8040)

Sandboxed Python execution for ad-hoc analysis. AST-based import restriction prevents unsafe imports (`os`, `subprocess`, etc.).

| Tool | Parameters | Returns |
|---|---|---|
| `execute_python` | `code` (str), `timeout` (int, default 30) | success, stdout, stderr, result |

**Restricted imports**: `os`, `subprocess`, `shutil`, `socket`, `ctypes`, `importlib`, `pickle`, `inspect`, `sys`

**Available libraries**: pandas (`pd`), numpy (`np`), math, json, datetime, random, statistics, itertools, collections, functools, typing

## Adding a New MCP Server

1. Create a new file in `mcp_servers/` (e.g., `mcp_servers/example_server.py`)
2. Define tools with `@app.tool()` decorators
3. Add a `get_app()` function returning `app.sse_app()`
4. Add the server URL to the relevant agent's `mcp_config.yaml`
5. Start with: `uvicorn mcp_servers.example_server:get_app --port XXXX`

```python
from mcp.server.fastmcp import FastMCP

app = FastMCP("example-mcp")

@app.tool()
async def my_tool(param: str) -> dict:
    """Tool description with Args and Returns."""
    return {"result": param}

_starlette_app = None

def get_app():
    global _starlette_app
    if _starlette_app is None:
        _starlette_app = app.sse_app()
    return _starlette_app
```

The tool is automatically discovered by any agent that connects to this server and calls `list_tools()`.
