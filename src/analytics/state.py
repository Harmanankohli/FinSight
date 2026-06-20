"""Analytics agent graph state — mutable container passed through pipeline nodes.

Each field is populated by a specific graph node as data flows through
the PydanticAI DAG: data_fetch → analyze → format_output → llm_summary.
"""

from dataclasses import dataclass, field


@dataclass
class AnalyticsState:
    """Mutable state carried through the analytics graph pipeline."""

    ticker: str = ""                                          # Target stock ticker
    period: str = "1y"                                        # Lookback period (e.g. 1y, 6mo)
    price_data: dict = field(default_factory=dict)            # {date: close_price}
    ohlcv_data: list[dict] = field(default_factory=list)      # Full OHLCV records
    fundamentals_data: dict = field(default_factory=dict)      # Fundamentals from MCP
    trend_analysis: dict | None = None                        # TrendAnalysis model dump
    forecast_result: dict | None = None                       # ForecastResult model dump
    chart_payloads: list[dict] = field(default_factory=list)  # ChartPayload list
    statistical_summary: dict | None = None                   # StatisticalSummary model dump
    anomaly_report: dict | None = None                        # AnomalyReport model dump
    analytics_signal: str = "neutral"                         # bull/bear/neutral
    analytics_confidence: float = 0.0                         # Signal confidence [0, 1]
    reasoning: str = ""                                       # LLM-generated prose summary
