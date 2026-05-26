import asyncio
import json
import logging
from collections.abc import AsyncIterable

from langfuse.langchain import CallbackHandler

from shared.base_agent import BaseAgent
from shared.mcp_client import MCPClient, MCPServerConfig
from shared.config import MCP_SERVER_URL, MCP_TIMEOUT, EVAL_ENABLED
from shared.observability import get_langfuse_client
from shared.runtime_eval import score_quant_response as _eval_quant_response
from shared.ticker_utils import extract_ticker, extract_holdings, validate_ticker_via_mcp, resolve_ticker_via_mcp
from shared.trace_context import extract_trace_ids

from .graph import QuantAnalysisGraph

logger = logging.getLogger(__name__)


class QuantAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Quant Analysis Agent",
            description="Computes quantitative risk metrics and financial analysis using MCP data and LangGraph",
            content_types=["text", "application/json"],
        )
        self.graph = QuantAnalysisGraph()
        self._mcp: MCPClient | None = None
        self._connected = False

    async def _ensure_connected(self):
        if not self._connected:
            self._mcp = MCPClient(configs=[MCPServerConfig(name="finsight-mcp", url=MCP_SERVER_URL)], timeout=MCP_TIMEOUT)
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
                self._mcp = None

    async def analyze(self, ticker: str, period: str = "5y", portfolio_holdings: list[str] | None = None, trace_ctx: dict | None = None) -> dict:
        logger.info("Quant analysis starting for %s (period=%s, holdings=%s, mcp_connected=%s)", ticker, period, portfolio_holdings, self._connected)
        await self._ensure_connected()

        langfuse_handler = CallbackHandler(trace_context=trace_ctx)
        result = await self.graph.run(
            ticker, period=period, portfolio_holdings=portfolio_holdings,
            mcp_client=self._mcp, langfuse_handler=langfuse_handler,
        )
        logger.info("Quant analysis complete for %s: rec=%s, dcf=%s",
                     ticker, result.get("recommendation"),
                     "ok" if result.get("dcf_valuation") else "null")
        return result

    async def _validate_ticker(self, ticker: str) -> tuple[bool, str, str]:
        if not ticker:
            return False, "", "Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V)."
        try:
            await self._ensure_connected()
            return await validate_ticker_via_mcp(self._mcp, ticker)
        except Exception as e:
            logger.warning("MCP ticker validation failed, proceeding with regex guess: %s", e)
            return True, ticker, ""

    async def _resolve_ticker(self, query: str, exclude_ticker: str = "") -> tuple[str, str]:
        try:
            await self._ensure_connected()
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
        try:
            with langfuse.start_as_current_observation(
                as_type="span",
                name="quant-agent-stream",
                input=query,
                trace_context=trace_ctx,
            ) as span:
                # Nest LangGraph CallbackHandler spans under this agent span
                if trace_id:
                    trace_ctx = {"trace_id": trace_id, "parent_span_id": span.id}

                ticker = extract_ticker(query)
                resolved = False

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

                holdings = extract_holdings(query, exclude_ticker=ticker)
                if holdings:
                    logger.info("Extracted portfolio holdings for %s: %s", ticker, holdings)

                try:
                    result = await self.analyze(ticker, portfolio_holdings=holdings, trace_ctx=trace_ctx)
                    span.update(output={"ticker": ticker, "recommendation": result.get("recommendation")})
                    if EVAL_ENABLED:
                        asyncio.create_task(
                            _eval_quant_response(
                                query,
                                result.get("reasoning", ""),
                                result,
                                trace_id,
                            )
                        )
                    yield {
                        "response_type": "data",
                        "is_task_complete": True,
                        "require_user_input": False,
                        "content": result,
                    }
                except Exception as e:
                    logger.exception("Quant analysis failed")
                    span.update(output={"error": str(e)})
                    yield {
                        "response_type": "text",
                        "is_task_complete": True,
                        "is_error": True,
                        "require_user_input": False,
                        "content": f"Quant analysis failed: {e}",
                    }
        finally:
            await self._disconnect()


