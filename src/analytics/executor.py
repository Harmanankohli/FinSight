import json
import logging
from collections.abc import AsyncIterable

from shared.agent_models import AnalyticsAgentOutput
from shared.base_agent import BaseAgent
from shared.logging_config import logged, logged_sync
from shared.mcp_client import get_shared_mcp
from shared.runtime_eval import score_analytics_deterministic
from shared.settings import EVAL_ENABLED
from shared.ticker_utils import resolve_and_validate_ticker

from .graph import AnalyticsPipeline

logger = logging.getLogger(__name__)


class AnalyticsAgent(BaseAgent):
    @logged_sync(log_args=False, log_result=False)
    def __init__(self):
        super().__init__(
            agent_name="Analytics Agent",
            description="Trend detection, forecasting, chart data, statistical analysis, and anomaly detection using PydanticAI",
            content_types=["text", "application/json"],
        )
        self.pipeline = AnalyticsPipeline()

    @logged()
    async def analyze(self, ticker: str, period: str = "1y", trace_ctx: dict | None = None) -> dict:
        logger.info("Analytics analysis starting for %s (period=%s)", ticker, period)
        mcp = await get_shared_mcp()
        result = await self.pipeline.run(ticker, period=period, mcp_client=mcp)
        validated = AnalyticsAgentOutput.model_validate(result)
        logger.info(
            "Analytics analysis complete for %s: signal=%s", ticker, validated.analytics_signal
        )
        return validated.model_dump()

    async def stream(self, query: str, context_id: str, task_id: str) -> AsyncIterable[dict]:
        logger.info("AnalyticsAgent.stream() called: query=%s...", query[:80])
        yield await self._build_response(query)

    @logged()
    async def _build_response(self, query: str) -> dict:
        async with self._telemetry_span("analytics-agent-stream", query) as (
            trace_ctx,
            span,
            trace_id,
        ):
            ticker, company = await resolve_and_validate_ticker(query)
            if not ticker:
                span.update(output={"error": company or "No ticker found"})
                return self._error_response(company or "Could not identify a stock ticker.")

            try:
                result = await self.analyze(ticker, trace_ctx=trace_ctx)
                schema_checks = score_analytics_deterministic(result)
                if not schema_checks.get("passed", False):
                    failing = [k for k, v in schema_checks.items() if k != "passed" and not v]
                    logger.warning(
                        "Analytics deterministic checks failed for %s: %s", ticker, failing
                    )
                result["schema_validation"] = schema_checks
                span.update(output={"ticker": ticker, "signal": result.get("analytics_signal")})
                if EVAL_ENABLED:
                    from shared.eval_gate import defer_eval
                    from shared.runtime_eval import score_analytics_response as _eval_analytics

                    defer_eval(
                        _eval_analytics,
                        query,
                        result.get("reasoning", json.dumps(result, default=str)),
                        result,
                        trace_id,
                    )
                return self._data_response(result)
            except Exception as e:
                logger.exception("Analytics analysis failed")
                span.update(output={"error": str(e)})
                return self._error_response(f"Analytics analysis failed: {e}")
