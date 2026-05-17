# MCP Server

Single unified MCP server (`mcp_servers/finsight_server.py`) hosting both agent registry and data tools.

## Overview

| Port | Tools | Registry |
|---|---|---|---|
| 8010 | `get_prices`, `get_financials`, `get_options_chain`, `get_company_filings`, `get_financial_filings`, `get_filing_content`, `validate_ticker`, `resolve_company_ticker`, `full_text_search`, `get_news_sentiment`, `get_earnings_calendar`, `execute_python` | `find_agent`, `resource://agent_cards/list`, `resource://agent_cards/{name}` |

No API keys required (SEC uses public API, news uses RSS feeds). Windows-compatible — `import resource` guarded by platform check.

## Running

```bash
python -m uvicorn mcp_servers.finsight_server:get_app --host 0.0.0.0 --port 8010
```

## Agent Registry

Agent cards loaded lazily from `agent_cards/*.json` on first tool call (no model download at import time), embedded via `sentence-transformers`, exposed as:

| Tool / Resource | Description |
|---|---|
| `find_agent(query)` | Semantic search via embedding dot-product |
| `resource://agent_cards/list` | Lists all available card URIs |
| `resource://agent_cards/{name}` | Retrieves specific agent card |

## Tools

### yfinance Tools

| Tool | Parameters | Returns |
|---|---|---|
| `get_prices` | `ticker`, `period` (1y), `interval` (1d) | OHLCV data array |
| `get_financials` | `ticker` | income_statement, balance_sheet, cash_flow, info |
| `get_options_chain` | `ticker`, `expiration` (optional) | Expiration dates or calls/puts |

### SEC EDGAR Tools

Public API — no key required.

| Tool | Parameters | Returns |
|---|---|---|
| `get_company_filings` | `ticker`, `form_types` (optional), `limit` (10) | Filing list with form, date, `edgar_url` (raw document), `ix_url` (viewer fallback) |
| `get_financial_filings` | `ticker`, `annual_limit` (5), `quarterly_limit` (8) | 10-K and 10-Q only, separated with pagination for sufficient coverage |
| `get_filing_content` | `edgar_url`, `ix_url` (optional) | Extracted text content from raw SEC filing URL, with fallback to IXBRL viewer |
| `full_text_search` | `query`, `ticker` (optional) | Relevance-scored SEC EDGAR search results |
| `validate_ticker` | `ticker` | Validates ticker against SEC database (cached CIK map) |
| `resolve_company_ticker` | `text` | Natural language company name to ticker (SEC reverse index + Yahoo fallback) |

**Note**: Requires valid `User-Agent` header. Uses `"FinSight Research (contact@finsight.com)"`. The RAG agent uses `get_filing_content` to fetch and index actual SEC filing content.

### Financial News Tools

RSS feeds: Yahoo Finance, CNBC, MarketWatch, Seeking Alpha. VADER sentiment scoring.

| Tool | Parameters | Returns |
|---|---|---|
| `get_news_sentiment` | `ticker`, `limit` (10) | Articles with sentiment scores, aggregates |
| `get_earnings_calendar` | `ticker` | Earnings date status |

### Python Runner Tool

Sandboxed execution with AST-based import restrictions. Runs in a separate subprocess (`-I -S` isolation mode) with optional `RLIMIT_CPU`/`RLIMIT_AS`/`RLIMIT_NOFILE` on non-Windows platforms.

| Tool | Parameters | Returns |
|---|---|---|
| `execute_python` | `code`, `timeout` (30) | success, stdout, stderr, result |

**Restricted imports**: `os`, `subprocess`, `shutil`, `socket`, `ctypes`, `importlib`, `pickle`, `inspect`, `sys`, `builtins`, `gc`, `threading`, `multiprocessing`, `signal`, `mmap`, `resource`, `pwd`, `grp`, `crypt`

**Available**: pandas, numpy, math, json, datetime, random, statistics, itertools, collections, functools, typing

## Adding a New Tool

```python
@app.tool()
async def my_tool(param: str) -> dict:
    """Tool description."""
    return {"result": param}
```

Auto-discovered by any agent that connects and calls `list_tools()`.

## MCP Client Usage

```python
from shared.mcp_client import MCPClient, MCPServerConfig

mcp = MCPClient(configs=[MCPServerConfig(name="finsight", url="http://localhost:8010/sse")])
await mcp.connect_all()
result = await mcp.call_tool_by_name("get_prices", {"ticker": "NVDA"})
```

### Parsing Tool Results

Use the `parse_mcp_result()` utility for consistent response handling:

```python
from shared.mcp_client import parse_mcp_result

result = await mcp.call_tool_by_name("get_company_filings", {"ticker": "NVDA"})
data = parse_mcp_result(result)  # Returns dict, list, str, or {"error": "..."}
```

Handles various MCP response formats including `.content` attributes with text parts, direct dicts, and JSON strings.
