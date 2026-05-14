import asyncio
import json
import logging
import os
import sys
from pathlib import Path

_src = str(Path(__file__).resolve().parent.parent.parent)
if _src not in sys.path:
    sys.path.insert(0, _src)

from google.adk import Agent
from shared.config import (
    A2A_TIMEOUT, ADK_MODEL, LLM_BASE_URL,
    AGENT_REGISTRY_URL, AGENT_SEED_URLS,
)

os.environ.setdefault("OPENAI_API_BASE", LLM_BASE_URL)
os.environ.setdefault("OPENAI_API_KEY", "ollama")

logger = logging.getLogger(__name__)

_discoverer = None
_discoverer_lock = asyncio.Lock()


async def _get_discoverer():
    global _discoverer
    from agent_1_adk.a2a_client import A2ADiscoverer

    async with _discoverer_lock:
        if _discoverer is not None:
            return _discoverer
        d = A2ADiscoverer(
            seed_urls=[u.strip() for u in AGENT_SEED_URLS.split(",") if u.strip()],
            request_timeout=A2A_TIMEOUT,
        )
        if AGENT_REGISTRY_URL:
            d.with_mcp_registry(AGENT_REGISTRY_URL)
        await d.discover()
        logger.info("Discovered %d skills: %s", len(d.list_skills()), list(d.list_skills().keys()))
        _discoverer = d
    return _discoverer


async def _call_skill(skill_id: str, params: dict) -> str:
    from agent_1_adk.a2a_client import A2AClient

    discoverer = await _get_discoverer()
    if not discoverer.find_agent(skill_id):
        logger.warning("Skill '%s' not found among %s, recreating discoverer...", skill_id, list(discoverer.list_skills().keys()))
        async with _discoverer_lock:
            _discoverer = None
        discoverer = await _get_discoverer()

    if not discoverer.find_agent(skill_id):
        logger.error("Skill '%s' still not found after re-discovery", skill_id)
        return json.dumps({"error": f"No agent available for {skill_id}"})

    client = A2AClient(timeout=A2A_TIMEOUT).with_discoverer(discoverer)
    result = await client.send_message(skill_id, params.get("query", ""), metadata=params)
    return json.dumps(result)


async def query_rag(ticker: str) -> str:
    """Retrieve SEC filings and financial documents for a stock ticker. Only call if the user explicitly asks about a publicly traded company.
    
    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT). Must be a valid US stock ticker.
    """
    return await _call_skill("sec_filing_retrieval", {"ticker": ticker, "query": f"Research {ticker}"})


async def query_quant(ticker: str) -> str:
    """Compute quantitative risk metrics (Sharpe ratio, Beta, VaR, volatility) for a stock ticker. Only call for investment analysis.
    
    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT). Must be a valid US stock ticker.
    """
    return await _call_skill("quant_analysis", {"ticker": ticker, "query": f"Analyze {ticker}", "period": "5y"})


async def query_sentiment(ticker: str) -> str:
    """Analyze market sentiment and insider signals for a stock ticker from news and SEC filings. Only call for stock-specific sentiment analysis.
    
    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT). Must be a valid US stock ticker.
    """
    return await _call_skill("sentiment_analysis", {"ticker": ticker, "query": f"Sentiment for {ticker}"})


root_agent = Agent(
    name="investment_orchestrator",
    model=ADK_MODEL,
    instruction=(
        "You are an investment research assistant.\n\n"
        "Tool call rules:\n"
        "1. ONLY call tools if the user explicitly asks about a stock or investment.\n"
        "2. For general chat, greetings (hello, hi), or non-stock queries (e.g. 'google', 'weather'), "
        "respond conversationally — do NOT call any tools.\n"
        "3. When a stock IS mentioned (e.g. 'Analyze NVDA', 'Should I invest in AAPL?'), "
        "call ALL THREE tools with the stock's ticker symbol as the 'ticker' parameter.\n"
        "4. If uncertain whether something is a ticker (e.g. 'google' is not a stock ticker), "
        "do NOT call tools — just respond conversationally.\n\n"
        "When calling tools, use these ticker values:\n"
        "- query_rag(ticker) — retrieves SEC filings\n"
        "- query_quant(ticker) — computes risk metrics\n"
        "- query_sentiment(ticker) — analyzes market sentiment\n\n"
        "After getting all three results, synthesize into a BUY/HOLD/SELL "
        "recommendation with a clear rationale."
    ),
    tools=[query_rag, query_quant, query_sentiment],
)
