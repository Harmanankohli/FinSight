import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import agent_1_adk.agent as mod
from agent_1_adk.sub_agent_client import SubAgentClient


def test_sync_discovery_no_seed_urls():
    c = SubAgentClient()
    c.discover_sync()
    assert c.list_agents() == []


def test_register_agent_stores_metadata():
    c = SubAgentClient()
    card = {
        "name": "RAG Agent",
        "description": "Retrieves SEC filings",
        "url": "http://localhost:8002",
        "skills": [
            {
                "id": "sec_filing_retrieval",
                "name": "SEC Filing Retrieval",
                "description": "Retrieves and analyzes SEC filings",
            }
        ],
    }
    c._register(card, "http://localhost:8002")
    agents = c.list_agents()
    assert len(agents) == 1
    assert agents[0]["name"] == "RAG Agent"
    assert "SEC Filing Retrieval" in agents[0]["skills"]


def test_list_skills():
    c = SubAgentClient()
    card = {
        "name": "Quant Agent",
        "url": "http://localhost:8003",
        "skills": [
            {
                "id": "quant_analysis",
                "name": "Quantitative Analysis",
                "description": "Computes risk metrics",
            }
        ],
    }
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
async def test_agent_tool_has_correct_name_and_doc():
    mock_client = MagicMock()
    mock_agents = [
        {"name": "RAG Agent", "description": "Retrieves filings", "skills": "sec"},
    ]

    with (
        patch.object(mod, "_client", mock_client),
        patch.object(mod, "_agent_list", mock_agents),
    ):
        fn = mod._make_agent_tool("RAG Agent", "Retrieves filings")
    assert fn.__name__ == "rag_agent"
    assert "Retrieves filings" in fn.__doc__


@pytest.mark.asyncio
async def test_agent_tool_calls_send_message():
    mock_client = MagicMock()
    mock_client.send_message = AsyncMock(
        return_value='{"result": "ok"}'
    )
    mock_agents = [
        {"name": "Quant Agent", "description": "Quant", "skills": "quant"},
    ]

    with (
        patch.object(mod, "_client", mock_client),
        patch.object(mod, "_agent_list", mock_agents),
    ):
        fn = mod._make_agent_tool("Quant Agent", "Quant")
        result = await fn(ticker="NVDA")

    mock_client.send_message.assert_awaited_once_with(
        "Quant Agent", "Research and analyze NVDA"
    )
    assert json.loads(result) == {"result": "ok"}
