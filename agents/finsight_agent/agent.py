import json
import logging
import os
import sys
from pathlib import Path

_src = str(Path(__file__).resolve().parent.parent.parent)
if _src not in sys.path:
    sys.path.insert(0, _src)

from google.adk import Agent

from agent_1_adk.a2a_client import A2AClient

logger = logging.getLogger(__name__)

_RAG_URL = os.environ.get("AGENT_LLAMAINDEX_URL", "http://localhost:8002")
_QUANT_URL = os.environ.get("AGENT_LANGGRAPH_URL", "http://localhost:8003")
_SENTIMENT_URL = os.environ.get("AGENT_CREWAI_URL", "http://localhost:8004")

a2a = A2AClient(timeout=45.0)


async def query_rag(ticker: str, query: str) -> str:
    result = await a2a.query_rag(_RAG_URL, ticker, query)
    return json.dumps(result)


async def query_quant(ticker: str, period: str = "5y") -> str:
    result = await a2a.query_quant(_QUANT_URL, ticker, period)
    return json.dumps(result)


async def query_sentiment(ticker: str) -> str:
    result = await a2a.query_sentiment(_SENTIMENT_URL, ticker)
    return json.dumps(result)


_MODEL = os.environ.get("ADK_MODEL", "groq/llama-3.3-70b-versatile")

root_agent = Agent(
    name="investment_orchestrator",
    model=_MODEL,
    instruction=(
        "You are an investment research assistant. "
        "Only use tools when the user asks about a specific stock ticker.\n"
        "For greetings or general chat, respond conversationally without calling tools.\n\n"
        "When the user asks about a stock (e.g. 'Should I invest in NVDA?'):\n"
        "1. Call `query_rag` to retrieve SEC filings\n"
        "2. Call `query_quant` for risk metrics\n"
        "3. Call `query_sentiment` for market sentiment\n"
        "4. Synthesize into a BUY/HOLD/SELL recommendation.\n\n"
        "Always respond conversationally in plain English, not JSON."
    ),
    tools=[query_rag, query_quant, query_sentiment],
)
