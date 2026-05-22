import asyncio
import json
import logging
from collections.abc import AsyncIterable

from shared.base_agent import BaseAgent
from shared.mcp_client import MCPClient, MCPServerConfig
from shared.config import MCP_SERVER_URL, MCP_TIMEOUT
from shared.observability import get_langfuse_client
from shared.ticker_utils import extract_ticker, validate_ticker_via_mcp, resolve_ticker_via_mcp
from shared.trace_context import extract_trace_ids

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

    async def _disconnect(self):
        """Gracefully disconnect MCP client to prevent connection errors."""
        if self._connected and self._mcp:
            try:
                await self._mcp.disconnect_all()
                logger.debug("MCP client disconnected gracefully")
            except Exception as e:
                logger.debug("Error during MCP disconnect (non-critical): %s", e)
            finally:
                self._connected = False

    async def _collect_data_parallel(self, ticker: str) -> dict:
        results = {}

        async def call(tool, args):
            try:
                r = await self._mcp.call_tool_by_name(tool, args)
                if hasattr(r, "content"):
                    for item in r.content:
                        txt = item.text if hasattr(item, "text") else str(item)
                        try:
                            return json.loads(txt)
                        except (json.JSONDecodeError, TypeError):
                            return {"raw": txt[:500]}
                return {"raw": str(r)[:500]}
            except Exception as e:
                return {"error": str(e)}

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
            return await validate_ticker_via_mcp(self._mcp, ticker)
        except Exception as e:
            logger.warning("MCP ticker validation failed, proceeding with regex guess: %s", e)
            return True, ticker, ""

    async def _resolve_ticker(self, query: str, exclude_ticker: str = "") -> tuple[str, str]:
        try:
            await self._connect()
            return await resolve_ticker_via_mcp(self._mcp, query, exclude_ticker)
        except Exception as e:
            logger.warning("MCP ticker resolution failed: %s", e)
            return "", ""

    async def stream(
        self, query: str, context_id: str, task_id: str
    ) -> AsyncIterable[dict]:
        trace_id, parent_span_id, query = extract_trace_ids(query)

        langfuse = get_langfuse_client()
        trace_ctx = (
            {"trace_id": trace_id, "parent_span_id": parent_span_id}
            if trace_id and parent_span_id
            else None
        )
        span = langfuse.start_observation(
            as_type="span",
            name="sentiment-agent-stream",
            input=query,
            trace_context=trace_ctx,
        )
        try:
            ticker = extract_ticker(query)
            resolved = False

            if not ticker:
                ticker, _ = await self._resolve_ticker(query)
                resolved = True

            if not ticker:
                span.update(output={"error": "No ticker found"})
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
                span.update(output={"error": f"Invalid ticker: {ticker}"})
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
                span.update(output={
                    "ticker": ticker,
                    "signal": result.get("overall_signal"),
                    "confidence": result.get("confidence_score"),
                })
                yield {
                    "response_type": "data",
                    "is_task_complete": True,
                    "require_user_input": False,
                    "content": result,
                }
            except Exception as e:
                logger.exception("Sentiment analysis failed")
                span.update(output={"error": str(e)})
                yield {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": f"Sentiment analysis failed: {e}",
                }
        finally:
            span.end()
            # Gracefully disconnect MCP client after stream completes
            await self._disconnect()
