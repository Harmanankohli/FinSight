# MCP Server

MCP server hosting both agent registry and data tools. Split from a single monolithic file (2095 lines) into per-tool modules (v1.41):

```
src/mcp_tools/
  ├── _app.py                # 78-line composition root with get_app() factory
  ├── finsight_server.py     # re-exports get_app() for backward compat
  ├── tools/
  │   ├── agent_registry.py  # find_agent(), resource://agent_cards/*
  │   ├── market_data.py     # get_prices(), get_financials(), get_options_chain()
  │   ├── edgar.py           # SEC EDGAR tools (filings, content, search)
  │   ├── ticker.py          # validate_ticker(), resolve_company_ticker()
  │   ├── sentiment.py       # news, sentiment indicators, earnings history
  │   └── sandbox.py         # execute_python() with AST gate + container mode
  └── infra/
      ├── rate_limiters.py   # TokenBucket rate limiter
      ├── embed.py           # sentence-transformers lazy loader
      └── news_fetch.py      # RSS feed fetchers (Yahoo, CNBC, MarketWatch, DDG fallback)
```

## Overview

| Port | Tools | Registry |
|---|---|---|
| 8010 | `get_prices`, `get_financials`, `get_options_chain`, `get_company_filings`, `get_financial_filings`, `get_filing_content`, `validate_ticker`, `resolve_company_ticker`, `full_text_search`, `get_news_sentiment`, `get_earnings_calendar`, `get_insider_transactions`, `get_peers`, `get_macro_indicators`, `get_scenario_shocks`, `get_sentiment_indicators`, `get_earnings_history`, `get_analyst_activity`, `get_valuation_timeseries`, `execute_python` | `find_agent`, `resource://agent_cards/list`, `resource://agent_cards/{name}` |

No API keys required (SEC uses public API, news uses RSS feeds). Windows-compatible — `import resource` guarded by platform check.

## Running

```bash
uv run python -m uvicorn mcp_tools.finsight_server:get_app --host 0.0.0.0 --port 8010
```

Health check: `GET http://localhost:8010/health` → `{"status":"ok","agent":"mcp"}`

## TTL Caching

`_TTLCache` class (OrderedDict + `time.monotonic()`) wraps each tool with zero new dependencies:

| Cache | TTL | Notes |
|---|---|---|
| `get_prices` | 1 min | Key: `(ticker, period, interval)` |
| `get_benchmark` | 1 h | Key: ticker — index benchmarks (^GSPC, ^VIX, etc.) |
| `get_financials` | 1 h | Key: `(ticker,)` |
| `get_news_sentiment` | 5 min | Only cached when articles found |
| `get_macro_indicators` | 15 min | Key: `"macro"` — Treasury yields, VIX, DXY, sector ETFs |
| `get_filing_content` | Permanent (LRU-200) | Filings are immutable |
| `_fetch_submissions` | 6 h | Shared by `get_company_filings` + `get_financial_filings` |
| `get_peers` | 24 h | yfinance Industry/Sector peer lists |
| `get_scenario_shocks` | 7 days | Historical crash returns per sector ETF |
| `get_insider_transactions` | Not cached | Insider data is queried on demand |
| `get_analyst_activity` | 1 h | Yahooquery grading_history |
| `get_valuation_timeseries` | 24 h | Yahooquery valuation_measures |
| `get_earnings_trend` | 1 h | Yahooquery earnings_trend forward estimates |

Cache hits log `"Cache hit for <tool>"` at DEBUG level. Cache misses fetch fresh data and store the result.

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

**Note**: Requires valid `User-Agent` header. Set via `SEC_USER_AGENT` env var in `.env` (format: `Your Name (your-email@example.com)`) — see `src/shared/settings.py`. The RAG agent uses `get_filing_content` to fetch and index actual SEC filing content.

### Financial News Tools

RSS feeds: Yahoo Finance, CNBC, MarketWatch, Seeking Alpha. VADER sentiment scoring.

| Tool | Parameters | Returns |
|---|---|---|
| `get_news_sentiment` | `ticker`, `limit` (10) | Articles with sentiment scores, aggregates |
| `get_earnings_calendar` | `ticker` | Earnings date status |

### Market Data Tools

| Tool | Parameters | Returns |
|---|---|---|
| `get_peers` | `ticker` | List of up to 8 peer tickers via yfinance Industry/Sector `top_companies`. Dynamic discovery — no static mapping needed. Cached 24h. |
| `get_macro_indicators` | — | Treasury yields (10Y, 2Y), VIX, DXY, yield-curve regime, sector ETF 1mo performance. Cached 15 min. |
| `get_scenario_shocks` | `sector` (optional) | Historical crash returns for 4 scenarios (2008, 2020, dot-com, 2022 mild recession) using sector-specific ETFs (QQQ/XLP/XLF/etc). Cached 7 days. |
| `get_insider_transactions` | `ticker`, `days` (90) | Structured insider buy/sell data from yfinance `Ticker.insider_transactions`. Returns `transactions` array + `summary` dict with total, buys, sells, direction, net_shares, net_value. |
| `get_sentiment_indicators` | `ticker` | Short interest %, analyst consensus breakdown (buy/hold/sell), institutional ownership %. |
| `get_earnings_history` | `ticker`, `limit` (8) | Last N quarters EPS estimates vs actuals, beat rate, average surprise %. Also returns `forward_estimates` (list of `{period, end_date, growth, eps_avg, ...}`) and `eps_revisions` (list of `{period, up_last_7d, up_last_30d, down_last_7d, down_last_30d}`) from yahooquery `earnings_trend`. yfinance/yahooquery failures handled independently. |
| `get_analyst_activity` | `ticker`, `limit` (20) | Recent analyst upgrade/downgrade/initiation history via yahooquery `grading_history`. Returns `activities[]` with firm, action, from/to grades, price target changes. Summary: upgrades/downgrades/initiations counts. Cached 1h. |
| `get_valuation_timeseries` | `ticker` | Quarterly valuation multiples history via yahooquery `valuation_measures`. Returns `periods[]` with pe_ratio, ps_ratio, pb_ratio, peg_ratio, ev_to_ebitda, ev_to_revenue, market_cap. Summary: pe_avg_2y, pe_current, pe_percentile. Cached 24h. |

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
from src.shared.mcp_client import MCPClient, MCPServerConfig

mcp = MCPClient(configs=[MCPServerConfig(name="finsight", url="http://localhost:8010/sse")])
await mcp.connect_all()
result = await mcp.call_tool_by_name("get_prices", {"ticker": "NVDA"})
```

### Service Auth (v1.43)

When `AUTH_ENABLED=true`, pass the service token via `MCPServerConfig`:

```python
config = MCPServerConfig(
    name="finsight",
    url="http://localhost:8010/sse",
    token=SERVICE_AUTH_TOKEN  # injected as Authorization: Bearer <token>
)
```

The MCP server wraps its SSE mount with `AuthMiddleware(accept={"service"})` — only valid service tokens can establish SSE connections.

### Parsing Tool Results

Use the `parse_mcp_result()` utility for consistent response handling:

```python
from src.shared.mcp_client import parse_mcp_result

result = await mcp.call_tool_by_name("get_company_filings", {"ticker": "NVDA"})
data = parse_mcp_result(result)  # Returns dict, list, str, or {"error": "..."}
```

Handles various MCP response formats including `.content` attributes with text parts, direct dicts, and JSON strings.
