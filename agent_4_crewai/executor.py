import logging
import re
from collections.abc import AsyncIterable

from shared.base_agent import BaseAgent
from shared.mcp_client import MCPClient, MCPServerConfig
from shared.config import MCP_SERVER_URL, MCP_TIMEOUT

from .crew import SentimentIntelligenceCrew
from .mcp_tools import MCPClientWrapper

logger = logging.getLogger(__name__)


class SentimentAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Sentiment Intelligence Agent",
            description="Synthesizes financial news sentiment, SEC insider trading signals, and investment narratives using CrewAI",
            content_types=["text", "application/json"],
        )
        self._mcp = MCPClient(configs=[MCPServerConfig(name="finsight-mcp", url=MCP_SERVER_URL)], timeout=MCP_TIMEOUT)
        self._wrapper = MCPClientWrapper(self._mcp)
        self._connected = False

    async def _connect(self):
        if not self._connected:
            await self._mcp.connect_all()
            self._connected = True

    async def _collect_data_parallel(self, ticker: str) -> dict:
        results = {}

        async def call(tool, args):
            try:
                r = await self._mcp.call_tool_by_name(tool, args)
                if hasattr(r, "content"):
                    for item in r.content:
                        txt = item.text if hasattr(item, "text") else str(item)
                        import json
                        try:
                            return json.loads(txt)
                        except (json.JSONDecodeError, TypeError):
                            return {"raw": txt[:500]}
                return {"raw": str(r)[:500]}
            except Exception as e:
                return {"error": str(e)}

        import asyncio, json
        await self._connect()

        tasks = {
            "news": call("get_news_sentiment", {"ticker": ticker, "limit": 5}),
            "filings": call("get_company_filings", {"ticker": ticker, "form_types": "", "limit": 3}),
        }
        done = await asyncio.gather(*[asyncio.create_task(t) for t in tasks.values()], return_exceptions=True)
        for name, result in zip(tasks.keys(), done):
            if isinstance(result, Exception):
                results[name] = {"error": str(result)}
            else:
                results[name] = result
        return results

    async def analyze(self, ticker: str, query_text: str) -> dict:
        data = await self._collect_data_parallel(ticker)
        logger.info("Collected data for %s: %s", ticker, list(data.keys()))
        crew_builder = SentimentIntelligenceCrew(self._wrapper)
        return await crew_builder.analyze(ticker, precollected_data=data)

    async def stream(
        self, query: str, context_id: str, task_id: str
    ) -> AsyncIterable[dict]:
        ticker_match = re.search(r"\b[A-Z]{2,5}\b", query)
        ticker = ticker_match.group(0) if ticker_match else ""

        try:
            result = await self.analyze(ticker, query)
            yield {
                "response_type": "data",
                "is_task_complete": True,
                "require_user_input": False,
                "content": result,
            }
        except Exception as e:
            logger.exception("Sentiment analysis failed")
            yield {
                "response_type": "text",
                "is_task_complete": True,
                "require_user_input": False,
                "content": f"Sentiment analysis failed: {e}",
            }
