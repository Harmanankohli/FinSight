import pytest
from unittest.mock import patch

from a2a.types import AgentCard, AgentSkill

from agent_1_adk.sub_agent_client import SubAgentClient


@pytest.mark.asyncio
async def test_discover_empty_seed_urls():
    with patch("agent_1_adk.sub_agent_client.AGENT_SEED_URLS", ""):
        c = SubAgentClient()
        await c.discover()
        assert c.list_agents() == []
        assert c.list_skills() == []


def test_register_multiple_agents():
    c = SubAgentClient()
    c._register(
        AgentCard(
            name="RAG Agent",
            skills=[
                AgentSkill(
                    id="sec_filing_retrieval",
                    name="SEC Filing Retrieval",
                    description="Retrieves SEC filings",
                )
            ],
        ),
        "http://localhost:8002",
    )
    c._register(
        AgentCard(
            name="Quant Agent",
            skills=[
                AgentSkill(
                    id="quant_analysis",
                    name="Quant Analysis",
                    description="Computes risk metrics",
                )
            ],
        ),
        "http://localhost:8003",
    )

    agents = c.list_agents()
    assert len(agents) == 2
    skills = c.list_skills()
    assert len(skills) == 2


@pytest.mark.asyncio
async def test_send_message_unknown_agent():
    c = SubAgentClient()
    result = await c.send_message("Nope", "task")
    assert "No agent found" in result
