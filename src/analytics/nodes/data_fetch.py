"""Data-fetching helpers for the analytics graph.

Retrieves price history and fundamentals from the shared MCP server.
Both functions gracefully handle failures by returning empty dicts so
downstream nodes never crash on missing data.
"""

import logging

logger = logging.getLogger(__name__)


async def _fetch_prices(mcp_client, ticker: str, period: str) -> dict:
    """Fetch OHLCV price data for a ticker over the given period.

    Returns dict with 'close_data' (date → price map) and 'ohlcv_data'
    (list of OHLCV records).  Keys are normalised to lowercase for
    compatibility with both Yahoo Finance and FMP data sources.
    """
    try:
        import json

        result = await mcp_client.call_tool_by_name(
            "get_prices", {"ticker": ticker, "period": period, "interval": "1d"}
        )
        data = {}
        if hasattr(result, "content"):
            for item in result.content:
                txt = item.text if hasattr(item, "text") else str(item)
                try:
                    data = json.loads(txt)
                except (json.JSONDecodeError, TypeError):
                    continue
        records = data.get("data", []) if isinstance(data, dict) else []
        close_data = {}
        ohlcv_data = []
        for row in records:
            if not isinstance(row, dict):
                continue
            date_str = row.get("Date") or row.get("date", "")
            close_val = row.get("Close") or row.get("close")
            if close_val is not None and date_str:
                close_data[date_str] = float(close_val)
            ohlcv_data.append(
                {
                    "date": date_str,
                    "open": float(row.get("Open", row.get("open", 0))),
                    "high": float(row.get("High", row.get("high", 0))),
                    "low": float(row.get("Low", row.get("low", 0))),
                    "close": float(row.get("Close", row.get("close", 0))),
                    "volume": float(row.get("Volume", row.get("volume", 0))),
                }
            )
        logger.info("Fetched prices for ticker=%s: %d days of data", ticker, len(close_data))
        return {"close_data": close_data, "ohlcv_data": ohlcv_data}
    except Exception as e:
        logger.warning("Failed to fetch prices for %s: %s", ticker, e)
        return {"close_data": {}, "ohlcv_data": []}


async def _fetch_fundamentals(mcp_client, ticker: str) -> dict:
    """Fetch fundamentals data (PE ratio, debt/equity, etc.) via MCP.

    Returns parsed JSON dict or empty dict on any error.
    """
    try:
        import json

        result = await mcp_client.call_tool_by_name("get_financials", {"ticker": ticker})
        if hasattr(result, "content"):
            for item in result.content:
                txt = item.text if hasattr(item, "text") else str(item)
                try:
                    return json.loads(txt)
                except (json.JSONDecodeError, TypeError):
                    continue
        logger.info("Fetched fundamentals for ticker=%s", ticker)
        return {}
    except Exception as e:
        logger.warning("Failed to fetch fundamentals for %s: %s", ticker, e)
        return {}
