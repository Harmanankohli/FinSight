import pytest
from unittest.mock import patch

from agent_1_adk.sub_agent_client import SubAgentClient


def test_discover_sync_no_seed_urls():
    with patch("agent_1_adk.sub_agent_client.AGENT_SEED_URLS", ""):
        c = SubAgentClient()
        c.discover_sync()
        assert c.list_agents() == []
        assert c.list_skills() == []


def test_register_multiple_agents():
    c = SubAgentClient()
    c._register(
        {
            "name": "RAG Agent",
            "url": "http://localhost:8002",
            "skills": [
                {
                    "id": "sec_filing_retrieval",
                    "name": "SEC Filing Retrieval",
                    "description": "Retrieves SEC filings",
                }
            ],
        },
        "http://localhost:8002",
    )
    c._register(
        {
            "name": "Quant Agent",
            "url": "http://localhost:8003",
            "skills": [
                {
                    "id": "quant_analysis",
                    "name": "Quant Analysis",
                    "description": "Computes risk metrics",
                }
            ],
        },
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
