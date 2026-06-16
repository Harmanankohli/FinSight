from dataclasses import dataclass, field


@dataclass
class AnalyticsState:
    ticker: str = ""
    period: str = "1y"
    price_data: dict = field(default_factory=dict)
    ohlcv_data: list[dict] = field(default_factory=list)
    fundamentals_data: dict = field(default_factory=dict)
    trend_analysis: dict | None = None
    forecast_result: dict | None = None
    chart_payloads: list[dict] = field(default_factory=list)
    statistical_summary: dict | None = None
    anomaly_report: dict | None = None
    analytics_signal: str = "neutral"
    analytics_confidence: float = 0.0
    reasoning: str = ""
