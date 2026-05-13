import json
import logging

from google.adk import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from shared.config import RAG_AGENT_URL, QUANT_AGENT_URL, SENTIMENT_AGENT_URL, A2A_TIMEOUT

from .a2a_client import A2AClient
from .intent_parser import parse_query
from .report_generator import synthesize

logger = logging.getLogger(__name__)

a2a = A2AClient(timeout=A2A_TIMEOUT)


async def _query_rag(ticker: str, query: str) -> str:
    result = await a2a.query_rag(RAG_AGENT_URL, ticker, query)
    return json.dumps(result)


async def _query_quant(ticker: str, period: str = "5y") -> str:
    result = await a2a.query_quant(QUANT_AGENT_URL, ticker, period)
    return json.dumps(result)


async def _query_sentiment(ticker: str) -> str:
    result = await a2a.query_sentiment(SENTIMENT_AGENT_URL, ticker)
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
    tools=[_query_rag, _query_quant, _query_sentiment],
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
    context = await parse_query(query, portfolio)

    rag_result = None
    quant_result = None
    sentiment_result = None

    if context.ticker:
        try:
            rag_result = await a2a.query_rag(_RAG_URL, context.ticker, query)
            quant_result = await a2a.query_quant(_QUANT_URL, context.ticker)
            sentiment_result = await a2a.query_sentiment(_SENTIMENT_URL, context.ticker)
        except Exception as e:
            logger.warning("Sub-agent calls partially failed: %s", e)

    brief = synthesize(context, rag_result, quant_result, sentiment_result)
    return brief.model_dump()
