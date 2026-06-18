import asyncio
import json
import logging

from pydantic_graph import BaseNode, Graph, GraphRunContext

from analytics.deps import AnalyticsDeps
from analytics.state import AnalyticsState
from analytics.nodes.data_fetch import _fetch_fundamentals, _fetch_prices
from analytics.nodes.trend import _detect_trends
from analytics.nodes.forecast import _run_forecast
from analytics.nodes.charts import _generate_charts
from analytics.nodes.statistics import _compute_statistics
from analytics.nodes.anomaly import _detect_anomalies
from analytics.nodes.summary import FormatOutputNode, LLMSummaryNode

logger = logging.getLogger(__name__)


class FetchDataNode(BaseNode[AnalyticsState, AnalyticsDeps]):
    async def run(self, ctx: GraphRunContext[AnalyticsState, AnalyticsDeps]) -> "AnalyzeNode":
        ticker = ctx.deps.ticker
        period = ctx.deps.period
        mcp = ctx.deps.mcp_client

        prices, fundamentals = await asyncio.gather(
            _fetch_prices(mcp, ticker, period),
            _fetch_fundamentals(mcp, ticker),
        )
        ctx.state.price_data = prices["close_data"]
        ctx.state.ohlcv_data = prices["ohlcv_data"]
        ctx.state.fundamentals_data = fundamentals
        logger.info(
            "FetchDataNode: ticker=%s prices=%d days fundamentals=%s",
            ticker,
            len(prices["close_data"]),
            bool(fundamentals),
        )
        return AnalyzeNode()


class AnalyzeNode(BaseNode[AnalyticsState, AnalyticsDeps]):
    async def run(self, ctx: GraphRunContext[AnalyticsState, AnalyticsDeps]) -> FormatOutputNode:
        trend, forecast, stats, anomalies, charts = await asyncio.gather(
            _detect_trends(ctx.state.price_data),
            _run_forecast(ctx.state.price_data),
            _compute_statistics(ctx.state.price_data, ctx.deps.mcp_client),
            _detect_anomalies(
                ctx.state.price_data, ctx.state.ohlcv_data, ctx.state.fundamentals_data
            ),
            _generate_charts(ctx.state.ohlcv_data, ctx.state.price_data),
        )
        ctx.state.trend_analysis = trend
        ctx.state.forecast_result = forecast
        ctx.state.statistical_summary = stats
        ctx.state.anomaly_report = anomalies
        ctx.state.chart_payloads = charts
        logger.info(
            "AnalyzeNode: ticker=%s trend=%s forecast_days=%d anomalies=%d charts=%d",
            ctx.deps.ticker,
            trend.get("trend_direction", "?"),
            len(forecast.get("forecast_prices", [])),
            anomalies.get("anomaly_count", 0),
            len(charts),
        )

        # Web search for anomaly catalysts if severity >= medium
        if anomalies.get("severity") in ("medium", "high") and ctx.deps.mcp_client:
            try:
                ticker = ctx.deps.ticker
                ar = await ctx.deps.mcp_client.call_tool_by_name(
                    "web_search",
                    {
                        "query": f"{ticker} stock price spike catalyst news",
                        "max_results": 3,
                        "time_filter": "w",
                    },
                )
                if hasattr(ar, "content") and ar.content:
                    raw = (
                        ar.content[0].text if hasattr(ar.content[0], "text") else str(ar.content[0])
                    )
                    data = json.loads(raw)
                    snippets = [
                        f"{r.get('title', '')}: {r.get('snippet', '')}"
                        for r in (data.get("results") or [])
                    ]
                    ctx.state.anomaly_report["catalyst_context"] = snippets
            except Exception as we:
                logger.debug("Anomaly catalyst search failed: %s", we)

        return FormatOutputNode()


class AnalyticsPipeline:
    def __init__(self):
        self.graph = Graph(
            nodes=[FetchDataNode, AnalyzeNode, FormatOutputNode, LLMSummaryNode],
        )

    async def run(self, ticker: str, period: str, mcp_client, langfuse_handler=None) -> dict:
        logger.info("Analytics graph run start: ticker=%s period=%s", ticker, period)
        deps = AnalyticsDeps(
            ticker=ticker,
            period=period,
            mcp_client=mcp_client,
            langfuse_handler=langfuse_handler,
        )
        state = AnalyticsState(ticker=ticker, period=period)
        result = await self.graph.run(FetchDataNode(), state=state, deps=deps)
        output = result.output if hasattr(result, "output") else result
        logger.info(
            "Analytics graph run complete: ticker=%s signal=%s",
            ticker,
            output.get("analytics_signal", "unknown") if isinstance(output, dict) else "unknown",
        )
        return output
