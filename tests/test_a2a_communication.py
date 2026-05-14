import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_1_adk.a2a_client import A2ADiscoverer


@pytest.fixture
def discoverer():
    return A2ADiscoverer(seed_urls=["http://localhost:8002"])


@pytest.mark.asyncio
async def test_discoverer_raises_when_no_sources():
    from agent_1_adk.a2a_client import A2ADiscoveryError
    d = A2ADiscoverer(seed_urls=[])
    with pytest.raises(A2ADiscoveryError, match="No discovery sources configured"):
        await d.discover()


@pytest.mark.asyncio
async def test_find_agent_returns_none_for_unknown_skill(discoverer):
    assert discoverer.find_agent("nonexistent") is None


@pytest.mark.asyncio
async def test_find_agent_by_query_matches_skill_keywords(discoverer):
    await discoverer.discover()
    match = discoverer.find_agent_by_query("SEC filings risk factors")
    if match:
        skill_id, entry = match
        assert "sec" in skill_id or "filing" in skill_id


@pytest.mark.asyncio
async def test_list_skills_returns_dict(discoverer):
    await discoverer.discover()
    skills = discoverer.list_skills()
    assert isinstance(skills, dict)


def test_intent_parser_extracts_ticker():
    from agent_1_adk.intent_parser import extract_ticker
    assert extract_ticker("Should I invest in NVDA?") == "NVDA"
    assert extract_ticker("What about AAPL?") == "AAPL"
    assert extract_ticker("no ticker here") is None


def test_intent_parser_extracts_risk_profile():
    from agent_1_adk.intent_parser import extract_risk_profile
    assert extract_risk_profile("conservative investor") == "conservative"
    assert extract_risk_profile("aggressive growth") == "aggressive"
    assert extract_risk_profile("moderate risk") == "moderate"


def test_report_generator_handles_none_data():
    from agent_1_adk.report_generator import synthesize
    from agent_1_adk.intent_parser import parse_query
    import asyncio
    context = asyncio.run(parse_query("Analyze NVDA", ["AAPL"]))
    brief = synthesize(context, rag_data=None, quant_data=None, sentiment_data=None)
    assert brief.final_recommendation in ("BUY", "HOLD", "SELL")
    assert brief.ticker == "NVDA"
    assert brief.disclaimer is not None
