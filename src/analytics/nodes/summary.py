import json
import logging

from pydantic_graph import BaseNode, End, GraphRunContext
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from analytics.deps import AnalyticsDeps
from analytics.state import AnalyticsState
from shared.llm_queue import Priority, llm_queue
from shared.settings import LLM_BASE_URL, LLM_SUMMARY_MODEL

logger = logging.getLogger(__name__)


class FormatOutputNode(BaseNode[AnalyticsState, AnalyticsDeps]):
    async def run(self, ctx: GraphRunContext[AnalyticsState, AnalyticsDeps]) -> "LLMSummaryNode":
        trend = ctx.state.trend_analysis or {}
        forecast = ctx.state.forecast_result or {}
        anomalies = ctx.state.anomaly_report or {}
        stats = ctx.state.statistical_summary or {}

        signals = []
        if trend.get("trend_direction") == "bullish":
            signals.append(1)
        elif trend.get("trend_direction") == "bearish":
            signals.append(-1)
        else:
            signals.append(0)

        if forecast.get("forecast_prices") and len(forecast["forecast_prices"]) > 0:
            last_close = list(ctx.state.price_data.values())[-1] if ctx.state.price_data else 0
            forecast_end = forecast["forecast_prices"][-1]
            if last_close > 0:
                forecast_change = (forecast_end - last_close) / last_close
                if forecast_change > 0.05:
                    signals.append(1)
                elif forecast_change < -0.05:
                    signals.append(-1)
                else:
                    signals.append(0)

        anomaly_severity = anomalies.get("severity", "none")
        if anomaly_severity in ("high", "medium"):
            signals.append(-1)

        if stats.get("return_distribution") == "leptokurtic":
            signals.append(-1)

        avg_signal = sum(signals) / len(signals) if signals else 0
        if avg_signal > 0.3:
            ctx.state.analytics_signal = "bullish"
        elif avg_signal < -0.3:
            ctx.state.analytics_signal = "bearish"
        else:
            ctx.state.analytics_signal = "neutral"
        ctx.state.analytics_confidence = round(abs(avg_signal), 2)

        return LLMSummaryNode()


class LLMSummaryNode(BaseNode[AnalyticsState, AnalyticsDeps]):
    async def run(self, ctx: GraphRunContext[AnalyticsState, AnalyticsDeps]) -> End[dict]:
        model = OpenAIModel(
            LLM_SUMMARY_MODEL,
            provider=OpenAIProvider(base_url=LLM_BASE_URL),
        )
        summary_agent = Agent(
            model=model,
            output_type=str,
            system_prompt="You are an analytics summary generator. Produce 3-4 sentences summarizing trend, forecast, statistical findings, and anomaly detections.",
        )

        analysis_data = {
            "ticker": ctx.state.ticker,
            "trend_analysis": ctx.state.trend_analysis,
            "forecast": ctx.state.forecast_result,
            "statistics": ctx.state.statistical_summary,
            "anomalies": ctx.state.anomaly_report,
            "signal": ctx.state.analytics_signal,
            "confidence": ctx.state.analytics_confidence,
        }

        prompt = f"Summarize this analysis for {ctx.state.ticker}:\n{json.dumps(analysis_data, indent=2)}"

        async with llm_queue.acquire(Priority.CRITICAL, "analytics-summary"):
            result = await summary_agent.run(prompt)

        ctx.state.reasoning = result.data if hasattr(result, "data") else str(result)

        return End(self._build_output(ctx.state))

    def _build_output(self, state: AnalyticsState) -> dict:
        return {
            "ticker": state.ticker,
            "trend_analysis": state.trend_analysis,
            "forecast": state.forecast_result,
            "charts": state.chart_payloads,
            "statistical_summary": state.statistical_summary,
            "anomalies": state.anomaly_report,
            "analytics_signal": state.analytics_signal,
            "analytics_confidence": state.analytics_confidence,
            "reasoning": state.reasoning,
        }
