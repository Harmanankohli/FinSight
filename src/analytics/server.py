"""FastMCP/ADK server entry point for the Analytics agent."""

# ruff: noqa: E402
import logging

from shared.bootstrap import bootstrap

_settings = bootstrap("analytics")

from shared.observability import init_instrumentation

init_instrumentation("analytics")

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from shared.agent_server import build_agent_app

from .executor import AnalyticsAgent

logger = logging.getLogger(__name__)

# Agent card: keep in sync with agent_cards/analytics.json
agent_card = AgentCard(
    name="Analytics Agent",
    description="Provides trend detection, forecasting, chart data, statistical analysis, and anomaly detection using PydanticAI and pydantic-graph",
    version="1.0.0",
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC",
            url=f"http://{_settings.host}:{_settings.agent_port_analytics}/a2a",
        )
    ],
    default_input_modes=["text", "text/plain"],
    default_output_modes=["text", "application/json"],
    skills=[
        AgentSkill(
            id="trend_detection",
            name="Trend Detection",
            description="Detect trend direction, MA crossovers (golden/death cross), and momentum shifts from price data",
            tags=["trend", "moving average", "momentum", "macd"],
            examples=["What is the current trend for AAPL?", "Detect MA crossovers for NVDA"],
        ),
        AgentSkill(
            id="forecasting",
            name="Price Forecasting",
            description="30-day exponential smoothing forecast with confidence bands and MAPE accuracy metric",
            tags=["forecast", "holt-winters", "prediction"],
            examples=[
                "Forecast NVDA price for the next 30 days",
                "What is the price prediction for MSFT?",
            ],
        ),
        AgentSkill(
            id="chart_generation",
            name="Chart Data Generation",
            description="Generate structured candlestick, line, and area chart payloads for frontend rendering",
            tags=["chart", "candlestick", "visualization"],
            examples=["Generate chart data for AAPL", "Create candlestick chart for TSLA"],
        ),
        AgentSkill(
            id="statistical_analysis",
            name="Statistical Analysis",
            description="Compute return distribution, skewness, kurtosis, Jarque-Bera test, and SPY correlation/beta",
            tags=["statistics", "skewness", "kurtosis", "beta", "correlation"],
            examples=["Run statistical analysis on MSFT returns", "What is AAPL's beta to SPY?"],
        ),
        AgentSkill(
            id="anomaly_detection",
            name="Anomaly Detection",
            description="Detect price spikes, volume anomalies, and fundamental outliers using z-score and IQR methods",
            tags=["anomaly", "outlier", "z-score"],
            examples=[
                "Detect anomalies in NVDA price data",
                "Are there any volume anomalies for TSLA?",
            ],
        ),
    ],
)


app = build_agent_app(
    agent_card=agent_card,
    agent=AnalyticsAgent(),
    service_name="analytics",
    accept=frozenset({"service"}),
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=_settings.agent_port_analytics)
