import json
import logging

from google.adk import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from shared.config import A2A_TIMEOUT, AGENT_REGISTRY_URL, AGENT_SEED_URLS

from .a2a_client import A2AClient, A2ADiscoverer
from .planner import parse_query, plan
from .report_generator import synthesize

logger = logging.getLogger(__name__)


async def _execute_skill(skill_id: str, params: dict) -> dict:
    discoverer = A2ADiscoverer(
        seed_urls=[u.strip() for u in AGENT_SEED_URLS.split(",") if u.strip()]
    )
    if AGENT_REGISTRY_URL:
        discoverer.with_mcp_registry(AGENT_REGISTRY_URL)
    await discoverer.discover()

    client = A2AClient(timeout=A2A_TIMEOUT).with_discoverer(discoverer)
    return await client.send_message(skill_id, params.get("query", ""), metadata=params)


async def query_rag(ticker: str, query: str = "") -> str:
    result = await _execute_skill("sec_filing_retrieval", {"ticker": ticker, "query": query or f"Research {ticker}"})
    return json.dumps(result)


async def query_quant(ticker: str, period: str = "5y") -> str:
    result = await _execute_skill("quant_analysis", {"ticker": ticker, "query": f"Analyze {ticker}", "period": period})
    return json.dumps(result)


async def query_sentiment(ticker: str) -> str:
    result = await _execute_skill("sentiment_analysis", {"ticker": ticker, "query": f"Sentiment for {ticker}"})
    return json.dumps(result)


orchestrator_agent = Agent(
    name="investment_orchestrator",
    instruction=(
        "You are an autonomous investment research orchestrator. "
        "When given an investment query:\n"
        "1. Extract the stock ticker, risk profile, and portfolio context from the query.\n"
        "2. Call 'query_rag' to retrieve financial documents and SEC filings.\n"
        "3. Call 'query_quant' to compute risk metrics and valuation.\n"
        "4. Call 'query_sentiment' to gather market sentiment and insider signals.\n"
        "5. Synthesize all results into a structured Investment Brief with a clear BUY/HOLD/SELL recommendation.\n"
        "Always respond with a complete Investment Brief in JSON format."
    ),
    tools=[query_rag, query_quant, query_sentiment],
    output_schema=dict,
)

_session_service = InMemorySessionService()
_runner = Runner(
    agent=orchestrator_agent,
    app_name="finsight",
    session_service=_session_service,
    auto_create_session=True,
)


async def run_investment_query(query: str, portfolio: list[str] | None = None) -> dict:
    context = parse_query(query, portfolio)

    rag_result = None
    quant_result = None
    sentiment_result = None

    if context.ticker:
        try:
            rag_result = await query_rag(context.ticker, query)
            quant_result = await query_quant(context.ticker)
            sentiment_result = await query_sentiment(context.ticker)
        except Exception as e:
            logger.warning("Sub-agent calls partially failed: %s", e)

    brief = synthesize(
        context,
        rag_data=json.loads(rag_result) if rag_result else None,
        quant_data=json.loads(quant_result) if quant_result else None,
        sentiment_data=json.loads(sentiment_result) if sentiment_result else None,
    )
    return brief.model_dump()
