import asyncio
import json
import logging
from collections.abc import AsyncIterable

from langfuse.langchain import CallbackHandler

from shared.base_agent import BaseAgent
from shared.config import EVAL_ENABLED
from shared.observability import get_langfuse_client
from shared.runtime_eval import score_quant_response as _eval_quant_response
from shared.ticker_utils import extract_ticker, extract_holdings, validate_ticker, resolve_ticker
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

    async def analyze(self, ticker: str, period: str = "5y", portfolio_holdings: list[str] | None = None, trace_ctx: dict | None = None) -> dict:
        logger.info("Quant analysis starting for %s (period=%s, holdings=%s)", ticker, period, portfolio_holdings)
        mcp = await get_shared_mcp()

        # Langfuse CallbackHandler is nested under the parent agent span via trace_ctx
        langfuse_handler = CallbackHandler(trace_context=trace_ctx)
        result = await self.graph.run(
            ticker, period=period, portfolio_holdings=portfolio_holdings,
            mcp_client=mcp, langfuse_handler=langfuse_handler,
        )
        logger.info("Quant analysis complete for %s: rec=%s, dcf=%s",
                     ticker, result.get("recommendation"),
                     "ok" if result.get("dcf_valuation") else "null")
        return result

    async def stream(
        self, query: str, context_id: str, task_id: str
    ) -> AsyncIterable[dict]:
        yield await self._build_response(query)

    async def _build_response(self, query: str) -> dict:
        trace_id, parent_span_id, query = extract_trace_ids(query)

        langfuse = get_langfuse_client()
        trace_ctx = (
            {"trace_id": trace_id, "parent_span_id": parent_span_id}
            if trace_id and parent_span_id
            else None
        )
        with langfuse.start_as_current_observation(
            as_type="span",
            name="quant-agent-stream",
            input=query,
            trace_context=trace_ctx,
        ) as span:
            # Nest LangGraph CallbackHandler under this agent span so graph steps appear as children
            if trace_id:
                trace_ctx = {"trace_id": trace_id, "parent_span_id": span.id}

            ticker = extract_ticker(query)
            resolved = False

            if not ticker:
                span.update(output={"error": "No ticker found"})
                return {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": "Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V).",
                }

            valid, validated_ticker, company = await validate_ticker(ticker)
            if not valid and not resolved:
                ticker, _ = await resolve_ticker(query, ticker)
                if ticker:
                    valid, validated_ticker, company = await validate_ticker(ticker)

            if not valid:
                span.update(output={"error": f"Invalid ticker: {ticker}"})
                return {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": f"Ticker '{ticker}' is not valid. Error: {company}",
                }

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
                return {
                    "response_type": "data",
                    "is_task_complete": True,
                    "require_user_input": False,
                    "content": result,
                }
            except Exception as e:
                logger.exception("Quant analysis failed")
                span.update(output={"error": str(e)})
                return {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": f"Quant analysis failed: {e}",
                }
