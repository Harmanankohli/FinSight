# ruff: noqa: E402
import logging

from shared.bootstrap import bootstrap

_settings = bootstrap("quant")

from shared.observability import init_instrumentation

init_instrumentation("quant")

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from shared.agent_server import build_agent_app
from shared.logging_config import logged

from .executor import QuantAgent

logger = logging.getLogger(__name__)

# Agent card: keep in sync with agent_cards/quant.json
agent_card = AgentCard(
    name="Quant Analysis Agent",
    description="Computes quantitative risk metrics, financial analysis, behavioral signals, and peer comparisons using yfinance and LangGraph",  # noqa: E501
    version="2.0.0",
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC",
            url=f"http://{_settings.host}:{_settings.agent_port_quant}/a2a",
        )
    ],
    default_input_modes=["text", "text/plain"],
    default_output_modes=["text", "application/json"],
    skills=[
        AgentSkill(
            id="quant_analysis",
            name="Quantitative Analysis",
            description="Compute Sharpe ratio, Beta, VaR, volatility, DCF valuation, stress tests, Monte Carlo simulation, and portfolio correlation",  # noqa: E501
            tags=[
                "quantitative",
                "risk metrics",
                "sharpe",
                "beta",
                "dcf",
                "valuation",
                "monte carlo",
            ],
            examples=[
                "Calculate risk metrics for NVDA",
                "Run a DCF valuation on AAPL",
                "Stress test MSFT under different market scenarios",
            ],
        ),
        AgentSkill(
            id="options_flow_analysis",
            name="Options Flow Analysis",
            description="Analyze put/call volume ratio, open interest imbalance, and unusual options activity to gauge market positioning",  # noqa: E501
            tags=[
                "options",
                "put/call",
                "open interest",
                "flow",
                "positioning",
                "unusual activity",
            ],
            examples=[
                "What's the options flow on NVDA?",
                "Is there unusual options activity in AAPL?",
                "Show me the put/call ratio for TSLA",
            ],
        ),
        AgentSkill(
            id="insider_transaction_analysis",
            name="Insider Transaction Analysis",
            description="Parse SEC Form 4 filings to detect insider buying/selling patterns, cluster activity, and net insider direction over 90 days",  # noqa: E501
            tags=["insider", "form 4", "sec", "insider buying", "insider selling"],
            examples=[
                "Are insiders buying AAPL?",
                "Show me recent insider transactions for MSFT",
                "Is there a cluster of insider selling at NVDA?",
            ],
        ),
        AgentSkill(
            id="positioning_signals",
            name="Positioning & Analyst Signals",
            description="Aggregate analyst consensus ratings, price target upside, short interest, and earnings surprise history into a composite positioning signal",  # noqa: E501
            tags=["analyst", "short interest", "price target", "consensus", "earnings surprise"],
            examples=[
                "What is analyst consensus on TSLA?",
                "What is the short interest on NVDA?",
                "Has AAPL been consistently beating earnings estimates?",
            ],
        ),
    ],
)


@logged()
async def _prewarm_llm():
    try:
        from langchain_openai import ChatOpenAI

        from shared.llm_queue import Priority, llm_queue
        from shared.settings import LLM_API_KEY, LLM_BASE_URL, LLM_SUMMARY_MODEL

        llm = ChatOpenAI(
            model=LLM_SUMMARY_MODEL,
            base_url=LLM_BASE_URL,
            api_key=LLM_API_KEY,
            temperature=0.0,
            max_tokens=1,
        )
        async with llm_queue.acquire(Priority.NORMAL, "quant-warmup"):
            await llm.ainvoke("ping")
        logger.info("LLM pre-warmed (%s)", LLM_SUMMARY_MODEL)
    except Exception as e:
        logger.warning("LLM warmup failed (non-fatal): %s", e)


app = build_agent_app(
    agent_card=agent_card,
    agent=QuantAgent(),
    service_name="quant",
    on_startup=[_prewarm_llm],
    accept=frozenset({"service"}),
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=_settings.agent_port_quant)
