import json
import logging
from typing import Any

from crewai.tools import BaseTool

logger = logging.getLogger(__name__)


class MCPClientWrapper:
    def __init__(self, mcp_client: Any):
        self._client = mcp_client

    async def call(self, server: str, tool: str, params: dict) -> Any:
        try:
            result = await self._client.call_tool(server, tool, params)
            if hasattr(result, "content") and isinstance(result.content, list):
                texts = []
                for item in result.content:
                    if hasattr(item, "text"):
                        texts.append(item.text)
                    elif isinstance(item, dict) and "text" in item:
                        texts.append(item["text"])
                return "\n".join(texts) if texts else result.content
            if isinstance(result, dict):
                return result
            if hasattr(result, "content"):
                return result.content
            return str(result)
        except Exception as e:
            logger.warning("MCP call %s/%s failed: %s", server, tool, e)
            return {"error": str(e)}


class RedditMCPTool(BaseTool):
    name: str = "Reddit Sentiment Fetcher"
    description: str = "Fetches Reddit posts and computes sentiment for a given stock ticker"

    def __init__(self, mcp_wrapper: MCPClientWrapper):
        super().__init__()
        self._mcp = mcp_wrapper

    def _run(self, ticker: str) -> str:
        import asyncio
        result = asyncio.run(
            self._mcp.call("reddit-mcp", "get_ticker_posts", {
                "ticker": ticker,
                "subreddits": ["stocks", "investing", "wallstreetbets"],
                "days_back": 30,
                "limit": 200,
            })
        )
        return json.dumps(result)


class TwitterMCPTool(BaseTool):
    name: str = "Twitter Sentiment Fetcher"
    description: str = "Fetches tweets mentioning a stock ticker"

    def __init__(self, mcp_wrapper: MCPClientWrapper):
        super().__init__()
        self._mcp = mcp_wrapper

    def _run(self, ticker: str) -> str:
        import asyncio
        result = asyncio.run(
            self._mcp.call("twitter-mcp", "search_tweets", {
                "query": f"${ticker}",
                "max_results": 100,
            })
        )
        return json.dumps(result)


class FinancialNewsMCPTool(BaseTool):
    name: str = "Financial News Fetcher"
    description: str = "Fetches financial news and analyst ratings for a ticker"

    def __init__(self, mcp_wrapper: MCPClientWrapper):
        super().__init__()
        self._mcp = mcp_wrapper

    def _run(self, ticker: str) -> str:
        import asyncio
        result = asyncio.run(
            self._mcp.call("financial-news", "get_news_sentiment", {
                "ticker": ticker,
            })
        )
        return json.dumps(result)


class SECInsiderMCPTool(BaseTool):
    name: str = "SEC Form 4 Reader"
    description: str = "Reads SEC Form 4 insider trading filings for a given company"

    def __init__(self, mcp_wrapper: MCPClientWrapper):
        super().__init__()
        self._mcp = mcp_wrapper

    def _run(self, ticker: str) -> str:
        import asyncio
        result = asyncio.run(
            self._mcp.call("sec-insider-mcp", "get_form4_filings", {
                "ticker": ticker,
                "months_back": 6,
            })
        )
        return json.dumps(result)
