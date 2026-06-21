"""FastMCP/ADK server entry point for the Market Context agent."""

# ruff: noqa: E402
import logging

from shared.bootstrap import bootstrap

_settings = bootstrap("market_context")

from shared.observability import init_instrumentation

init_instrumentation("market_context")

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from shared.agent_server import build_agent_app

from .executor import MarketContextAgent

logger = logging.getLogger(__name__)

# Agent card: keep in sync with agent_cards/market_context.json
agent_card = AgentCard(
    name="Market Context Agent",
    description="Provides macro regime analysis (yield curve, VIX, DXY, sector rotation) and competitive peer landscape positioning using CrewAI",  # noqa: E501
    version="2.0.0",
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC",
            url=f"http://{_settings.host}:{_settings.agent_port_market}/a2a",
        )
    ],
    default_input_modes=["text", "text/plain"],
    default_output_modes=["text", "application/json"],
    skills=[
        AgentSkill(
            id="macro_regime_analysis",
            name="Macro Regime Analysis",
            description="Assess current macro environment (yield curve regime, VIX volatility, DXY dollar strength, sector ETF rotation) and determine if it favours or penalises the target stock",  # noqa: E501
            tags=["macro", "yield curve", "VIX", "DXY", "sector rotation", "regime"],
            examples=[
                "What is the macro environment for NVDA?",
                "Does the current rate regime favour AAPL?",
            ],
        ),
        AgentSkill(
            id="peer_landscape_analysis",
            name="Competitive Peer Landscape",
            description="Compare target stock against sector peers on growth, margins, and valuation to identify relative positioning headwinds and tailwinds",  # noqa: E501
            tags=["peers", "competitive", "valuation", "margins", "growth", "sector"],
            examples=[
                "How does MSFT compare to its software peers?",
                "Is TSLA expensive relative to auto sector peers?",
            ],
        ),
    ],
)

app = build_agent_app(
    agent_card=agent_card,
    agent=MarketContextAgent(),
    service_name="market_context",
    accept=frozenset({"service"}),
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=_settings.host, port=_settings.agent_port_market)
