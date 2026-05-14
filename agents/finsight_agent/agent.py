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


async def _get_discoverer():
    global _discoverer
    from agent_1_adk.a2a_client import A2ADiscoverer

    if _discoverer is None:
        _discoverer = A2ADiscoverer(
            seed_urls=[u.strip() for u in AGENT_SEED_URLS.split(",") if u.strip()],
            request_timeout=A2A_TIMEOUT,
        )
        if AGENT_REGISTRY_URL:
            _discoverer.with_mcp_registry(AGENT_REGISTRY_URL)
        await _discoverer.discover()
        logger.info("Discovered agents: %s", list(_discoverer.list_skills().keys()))
    return _discoverer


async def _call_skill(skill_id: str, params: dict) -> str:
    from agent_1_adk.a2a_client import A2AClient, A2ADiscoveryError

    discoverer = await _get_discoverer()
    if not discoverer.find_agent(skill_id):
        logger.warning("Skill '%s' not cached, re-discovering...", skill_id)
        _discoverer = None
        discoverer = await _get_discoverer()

    client = A2AClient(timeout=A2A_TIMEOUT).with_discoverer(discoverer)
    result = await client.send_message(skill_id, params.get("query", ""), metadata=params)
    return json.dumps(result)


async def query_rag(ticker: str) -> str:
    return await _call_skill("sec_filing_retrieval", {"ticker": ticker, "query": f"Research {ticker}"})


async def query_quant(ticker: str) -> str:
    return await _call_skill("quant_analysis", {"ticker": ticker, "query": f"Analyze {ticker}", "period": "5y"})


async def query_sentiment(ticker: str) -> str:
    return await _call_skill("sentiment_analysis", {"ticker": ticker, "query": f"Sentiment for {ticker}"})


root_agent = Agent(
    name="investment_orchestrator",
    model=ADK_MODEL,
    instruction=(
        "You are an investment research assistant. "
        "When the user asks about a stock (e.g. 'Should I invest in NVDA?'), "
        "ALWAYS call ALL THREE tools: first query_rag, then query_quant, "
        "then query_sentiment. Each requires a 'ticker' parameter (e.g. NVDA, AAPL). "
        "After getting all three results, synthesize into a BUY/HOLD/SELL "
        "recommendation with a clear rationale. For greetings, just respond "
        "conversationally without calling any tools."
    ),
    tools=[query_rag, query_quant, query_sentiment],
)
