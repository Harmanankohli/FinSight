import asyncio
import logging

from pydantic_graph import Graph

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


class FetchDataNode:
    async def run(self, ctx) -> "AnalyzeNode":
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
        return AnalyzeNode()


class AnalyzeNode:
    async def run(self, ctx) -> FormatOutputNode:
        trend, forecast, stats, anomalies, charts = await asyncio.gather(
            _detect_trends(ctx.state.price_data),
            _run_forecast(ctx.state.price_data),
            _compute_statistics(ctx.state.price_data, ctx.deps.mcp_client),
            _detect_anomalies(ctx.state.price_data, ctx.state.ohlcv_data, ctx.state.fundamentals_data),
            _generate_charts(ctx.state.ohlcv_data, ctx.state.price_data),
        )
        ctx.state.trend_analysis = trend
        ctx.state.forecast_result = forecast
        ctx.state.statistical_summary = stats
        ctx.state.anomaly_report = anomalies
        ctx.state.chart_payloads = charts
        return FormatOutputNode()


class AnalyticsPipeline:
    def __init__(self):
        self.graph = Graph(
            nodes=[FetchDataNode, AnalyzeNode, FormatOutputNode, LLMSummaryNode],
        )

    async def run(self, ticker: str, period: str, mcp_client, langfuse_handler=None) -> dict:
        deps = AnalyticsDeps(
            ticker=ticker,
            period=period,
            mcp_client=mcp_client,
            langfuse_handler=langfuse_handler,
        )
        state = AnalyticsState(ticker=ticker, period=period)
        result = await self.graph.run(FetchDataNode(), state=state, deps=deps)
        return result.output if hasattr(result, "output") else result
