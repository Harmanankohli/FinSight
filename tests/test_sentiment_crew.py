import json

import pytest
from unittest.mock import MagicMock

from agent_4_crewai.mcp_tools import MCPClientWrapper, RedditMCPTool, SECInsiderMCPTool


class MockContent:
    def __init__(self, data: dict):
        self.content = data


class MockMCPClient:
    async def call_tool(self, server, tool, arguments):
        if tool == "get_ticker_posts":
            return MockContent({
                "ticker": arguments.get("ticker"),
                "sentiment_score": 0.25,
                "total_posts": 50,
                "positive_posts": 20,
                "negative_posts": 10,
                "posts": [],
            })
        if tool == "get_form4_filings":
            return MockContent({
                "ticker": arguments.get("ticker"),
                "insider_activity": "No unusual activity",
                "signal": "neutral",
            })
        return MockContent({"error": "unknown tool"})


@pytest.fixture
def mcp_wrapper():
    return MCPClientWrapper(MockMCPClient())


def test_reddit_tool(mcp_wrapper):
    tool = RedditMCPTool(mcp_wrapper)
    result = tool._run("NVDA")
    data = json.loads(result)
    assert data["ticker"] == "NVDA"
    assert data["sentiment_score"] == 0.25
    assert data["total_posts"] == 50


def test_insider_tool(mcp_wrapper):
    tool = SECInsiderMCPTool(mcp_wrapper)
    result = tool._run("AAPL")
    data = json.loads(result)
    assert data["ticker"] == "AAPL"
    assert "signal" in data


def test_mcp_client_wrapper_error_handling(mcp_wrapper):
    import asyncio
    result = asyncio.run(mcp_wrapper.call("unknown-server", "unknown-tool", {}))
    assert "error" in result or "content" in str(type(result))


def test_sentiment_crew_build(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key")
    from agent_4_crewai.crew import SentimentIntelligenceCrew
    crew_builder = SentimentIntelligenceCrew(MCPClientWrapper(MockMCPClient()))
    crew = crew_builder.build_crew("TSLA")
    assert len(crew.agents) == 4
    assert len(crew.tasks) == 4
    assert crew.process == "sequential"
