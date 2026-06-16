import logging

logger = logging.getLogger(__name__)


async def _fetch_prices(mcp_client, ticker: str, period: str) -> dict:
    try:
        result = await mcp_client.call_tool_by_name(
            "get_prices", {"ticker": ticker, "period": period, "interval": "1d"}
        )
        content = result.get("content", [])
        data = {}
        for item in content:
            if isinstance(item, dict) and "text" in item:
                try:
                    import json
                    data = json.loads(item["text"])
                except (json.JSONDecodeError, TypeError):
                    continue
        raw = data if isinstance(data, dict) else {}
        close_data = {}
        ohlcv_data = []
        for date_str, row in raw.items():
            if isinstance(row, dict):
                close_val = row.get("Close") or row.get("close")
                if close_val is not None:
                    close_data[date_str] = float(close_val)
                ohlcv_row = {
                    "date": date_str,
                    "open": float(row.get("Open", row.get("open", 0))),
                    "high": float(row.get("High", row.get("high", 0))),
                    "low": float(row.get("Low", row.get("low", 0))),
                    "close": float(row.get("Close", row.get("close", 0))),
                    "volume": float(row.get("Volume", row.get("volume", 0))),
                }
                ohlcv_data.append(ohlcv_row)
        return {"close_data": close_data, "ohlcv_data": ohlcv_data}
    except Exception as e:
        logger.warning("Failed to fetch prices for %s: %s", ticker, e)
        return {"close_data": {}, "ohlcv_data": []}


async def _fetch_fundamentals(mcp_client, ticker: str) -> dict:
    try:
        result = await mcp_client.call_tool_by_name(
            "get_financials", {"ticker": ticker}
        )
        content = result.get("content", [])
        for item in content:
            if isinstance(item, dict) and "text" in item:
                try:
                    import json
                    return json.loads(item["text"])
                except (json.JSONDecodeError, TypeError):
                    continue
        return {}
    except Exception as e:
        logger.warning("Failed to fetch fundamentals for %s: %s", ticker, e)
        return {}
