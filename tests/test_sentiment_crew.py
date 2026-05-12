import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from agent_4_crewai.mcp_tools import MCPClientWrapper


class MockContent:
    def __init__(self, data: dict):
        self.content = data


class MockMCPClient:
    async def list_tools(self):
        return []

    async def call_tool_by_name(self, tool_name, arguments):
        return MockContent({"result": "ok", "tool": tool_name})


@pytest.fixture
def mcp_wrapper():
    return MCPClientWrapper(MockMCPClient())


@pytest.mark.asyncio
async def test_discover_tools_returns_empty_when_no_tools(mcp_wrapper):
    tools = await mcp_wrapper.discover_tools()
    assert tools == []


@pytest.mark.asyncio
async def test_call_by_name(mcp_wrapper):
    result = await mcp_wrapper.call_by_name("test_tool", {"key": "val"})
    assert "result" in str(result) or "result" in str(type(result))


def test_sentiment_crew_build(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key")
    from agent_4_crewai.crew import SentimentIntelligenceCrew
    crew_builder = SentimentIntelligenceCrew(MCPClientWrapper(MockMCPClient()))
    crew = crew_builder.build_crew("TSLA")
    assert len(crew.agents) == 4
    assert len(crew.tasks) == 4
    assert crew.process == "sequential"
