import logging
from collections.abc import AsyncIterable

from shared.base_agent import BaseAgent
from shared.mcp_client import MCPClient, MCPServerConfig
from shared.config import MCP_SERVER_URL, MCP_TIMEOUT
from shared.ticker_utils import clean_query_for_resolution, extract_ticker, is_valid_ticker_format

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

    async def _validate_ticker(self, ticker: str) -> tuple[bool, str, str]:
        if not ticker:
            return False, "", "Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V)."
        try:
            await self._connect()
        except Exception as e:
            logger.warning("MCP connect failed, proceeding with regex ticker guess: %s", e)
            return True, ticker, ""
        try:
            import json
            result = await self._mcp.call_tool_by_name("validate_ticker", {"ticker": ticker})
            if hasattr(result, "content"):
                for item in result.content:
                    try:
                        v = json.loads(item.text if hasattr(item, "text") else str(item))
                        if v.get("valid"):
                            return True, v.get("ticker", ticker), v.get("company_name", "")
                        return False, ticker, v.get("error", "Ticker not found in SEC database")
                    except Exception:
                        continue
            return False, ticker, "Ticker not found in SEC database"
        except Exception as e:
            logger.warning("Ticker validation via MCP failed, proceeding with regex guess: %s", e)
            return True, ticker, ""

    async def _resolve_ticker(self, query: str, exclude_ticker: str = "") -> tuple[str, str]:
        cleaned = clean_query_for_resolution(query)
        if exclude_ticker:
            cleaned = cleaned.replace(exclude_ticker, "").strip()
        if not cleaned:
            cleaned = query
        try:
            await self._connect()
        except Exception as e:
            logger.warning("MCP connect failed during ticker resolution: %s", e)
            return "", ""
        try:
            import json
            result = await self._mcp.call_tool_by_name("resolve_company_ticker", {"text": cleaned})
            if hasattr(result, "content"):
                for item in result.content:
                    try:
                        v = json.loads(item.text if hasattr(item, "text") else str(item))
                        ticker = v.get("ticker", "")
                        if ticker and is_valid_ticker_format(ticker):
                            return ticker, v.get("company_name", "")
                    except Exception:
                        continue
        except Exception as e:
            logger.warning("MCP ticker resolution failed: %s", e)
        return "", ""

    async def stream(
        self, query: str, context_id: str, task_id: str
    ) -> AsyncIterable[dict]:
        ticker = extract_ticker(query)
        resolved = False

        if not ticker:
            ticker, _ = await self._resolve_ticker(query)
            resolved = True

        if not ticker:
            yield {
                "response_type": "text",
                "is_task_complete": True,
                "is_error": True,
                "require_user_input": False,
                "content": "Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V).",
            }
            return

        valid, validated_ticker, company = await self._validate_ticker(ticker)
        if not valid and not resolved:
            ticker, _ = await self._resolve_ticker(query, exclude_ticker=ticker)
            if ticker:
                valid, validated_ticker, company = await self._validate_ticker(ticker)

        if not valid:
            yield {
                "response_type": "text",
                "is_task_complete": True,
                "is_error": True,
                "require_user_input": False,
                "content": f"Ticker '{ticker}' is not valid. Error: {company}",
            }
            return

        ticker = validated_ticker

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
                "is_error": True,
                "require_user_input": False,
                "content": f"Sentiment analysis failed: {e}",
            }
