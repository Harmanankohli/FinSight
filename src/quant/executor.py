"""Quantitative analysis agent — orchestrates MCP-powered financial data retrieval
and LangGraph-based multi-stage analysis for a given ticker."""

import logging
from collections.abc import AsyncIterable

from langfuse.langchain import CallbackHandler

from shared.base_agent import BaseAgent
from shared.logging_config import logged, logged_sync
from shared.mcp_client import get_shared_mcp
from shared.runtime_eval import score_quant_deterministic
from shared.settings import EVAL_ENABLED
from shared.ticker_utils import extract_holdings, resolve_and_validate_ticker

from .graph import QuantAnalysisGraph

logger = logging.getLogger(__name__)


class QuantAgent(BaseAgent):
    """Agent that runs quantitative risk and financial analysis on a stock ticker.
    Delegates to :class:`QuantAnalysisGraph` for the actual pipeline execution and
    wraps results with telemetry, schema validation, and deferred evaluation."""

    @logged_sync(log_args=False, log_result=False)
    def __init__(self):
        """Initialise the QuantAgent with a pre-built QuantAnalysisGraph."""
        super().__init__(
            agent_name="Quant Analysis Agent",
            description="Computes quantitative risk metrics and financial analysis using MCP data and LangGraph",  # noqa: E501
            content_types=["text", "application/json"],
        )
        self.graph = QuantAnalysisGraph()

    @logged()
    async def analyze(
        self,
        ticker: str,
        period: str = "5y",
        portfolio_holdings: list[str] | None = None,
        trace_ctx: dict | None = None,
    ) -> dict:
        """Run the full quant pipeline for *ticker* and return structured results.

        Acquires the shared MCP client, wires a Langfuse handler from the parent
        trace context, and delegates to ``QuantAnalysisGraph.run()``."""
        logger.info(
            "Quant analysis starting for %s (period=%s, holdings=%s)",
            ticker,
            period,
            portfolio_holdings,
        )
        mcp = await get_shared_mcp()

        # Langfuse CallbackHandler is nested under the parent agent span via trace_ctx
        langfuse_handler = CallbackHandler(trace_context=trace_ctx)
        result = await self.graph.run(
            ticker,
            period=period,
            portfolio_holdings=portfolio_holdings,
            mcp_client=mcp,
            langfuse_handler=langfuse_handler,
        )
        logger.info(
            "Quant analysis complete for %s: rec=%s, dcf=%s",
            ticker,
            result.get("recommendation"),
            "ok" if result.get("dcf_valuation") else "null",
        )
        return result

    async def stream(self, query: str, context_id: str, task_id: str) -> AsyncIterable[dict]:
        """Stream interface required by :class:`BaseAgent` — resolves the ticker and yields a single response dict."""
        logger.info("QuantAgent.stream() called: query=%s...", query[:80])
        yield await self._build_response(query)

    @logged()
    async def _build_response(self, query: str) -> dict:
        """Resolve the ticker, run analysis, attach schema validation, and return a data or error response."""
        async with self._telemetry_span("quant-agent-stream", query) as (trace_ctx, span, trace_id):
            ticker, company = await resolve_and_validate_ticker(query)
            if not ticker:
                span.update(output={"error": company or "No ticker found"})
                return self._error_response(company or "Could not identify a stock ticker.")

            holdings = extract_holdings(query, exclude_ticker=ticker)
            if holdings:
                logger.info("Extracted portfolio holdings for %s: %s", ticker, holdings)

            try:
                result = await self.analyze(
                    ticker, portfolio_holdings=holdings, trace_ctx=trace_ctx
                )
                schema_checks = score_quant_deterministic(result)
                if not schema_checks.get("passed", False):
                    failing = [k for k, v in schema_checks.items() if k != "passed" and not v]
                    logger.warning(
                        "Quant deterministic schema validation failed for %s: %s", ticker, failing
                    )
                result["schema_validation"] = schema_checks
                span.update(
                    output={
                        "ticker": ticker,
                        "recommendation": result.get("recommendation"),
                        "schema_passed": schema_checks["passed"],
                    }
                )
                if EVAL_ENABLED:
                    from shared.eval_gate import defer_eval
                    from shared.runtime_eval import score_quant_response as _eval_quant_response

                    defer_eval(
                        _eval_quant_response,
                        query,
                        result.get("reasoning", ""),
                        result,
                        trace_id,
                    )
                return self._data_response(result)
            except Exception as e:
                logger.exception("Quant analysis failed")
                span.update(output={"error": str(e)})
                return self._error_response(f"Quant analysis failed: {e}")
