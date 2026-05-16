import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from a2a.types import AgentCard, AgentSkill

from agent_1_adk.sub_agent_client import SubAgentClient


@pytest.mark.asyncio
async def test_discovery_no_seed_urls():
    with patch("agent_1_adk.sub_agent_client.AGENT_SEED_URLS", ""):
        c = SubAgentClient()
        await c.discover()
        assert c.list_agents() == []


def test_register_agent_stores_metadata():
    c = SubAgentClient()
    card = AgentCard(
        name="RAG Agent",
        description="Retrieves SEC filings",
        skills=[
            AgentSkill(
                id="sec_filing_retrieval",
                name="SEC Filing Retrieval",
                description="Retrieves and analyzes SEC filings",
            )
        ],
    )
    c._register(card, "http://localhost:8002")
    agents = c.list_agents()
    assert len(agents) == 1
    assert agents[0]["name"] == "RAG Agent"
    assert "SEC Filing Retrieval" in agents[0]["skills"]


def test_list_skills():
    c = SubAgentClient()
    card = AgentCard(
        name="Quant Agent",
        skills=[
            AgentSkill(
                id="quant_analysis",
                name="Quantitative Analysis",
                description="Computes risk metrics",
            )
        ],
    )
    c._register(card, "http://localhost:8003")
    skills = c.list_skills()
    assert len(skills) == 1
    assert skills[0]["agent_name"] == "Quant Agent"
    assert skills[0]["skill_id"] == "quant_analysis"


@pytest.mark.asyncio
async def test_send_message_unknown_agent():
    c = SubAgentClient()
    result = await c.send_message("UnknownAgent", "do something")
    assert "error" in result
    assert "UnknownAgent" in result


@pytest.mark.asyncio
async def test_send_message_no_data():
    """Client with no agents returns error, not exception."""
    c = SubAgentClient()
    result = await c.send_message("Nope", "task")
    assert "No agent found" in result
