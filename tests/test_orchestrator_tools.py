"""Tests for the orchestrator agent's ``send_message`` A2A tool."""

import pytest
from unittest.mock import AsyncMock, patch

from a2a.types import AgentCard, AgentSkill


@pytest.fixture
def patched_client():
    with patch("agent_1_adk.agent._client") as mock:
        mock.list_agents.return_value = [
            {
                "name": "RAG Agent",
                "description": "SEC filings agent",
                "skills": "sec_filing_retrieval",
            },
        ]
        mock.send_message = AsyncMock(
            return_value='{"result": "analysis complete"}'
        )
        yield mock


class TestSendMessage:
    """send_message delegates to SubAgentClient.send_message."""

    @pytest.mark.asyncio
    async def test_sends_to_named_agent(self, patched_client):
        from agent_1_adk.agent import send_message

        mock_tool_context = AsyncMock()
        mock_tool_context.state = {}

        result = await send_message(
            "RAG Agent", "Research NVDA", mock_tool_context
        )

        patched_client.send_message.assert_awaited_once_with(
            "RAG Agent", "Research NVDA"
        )
        assert "analysis complete" in result

    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_agent(self):
        with patch("agent_1_adk.agent._client") as mock:
            mock.send_message = AsyncMock(
                return_value='{"error": "No agent found"}'
            )
            from agent_1_adk.agent import send_message

            mock_tool_context = AsyncMock()
            mock_tool_context.state = {}

            result = await send_message(
                "Unknown", "task", mock_tool_context
            )
            assert "error" in result


class TestRootAgent:
    """Root agent is created with proper tools."""

    def test_has_only_send_message_tool(self):
        from agent_1_adk.agent import root_agent

        tool_names = {t.__name__ for t in root_agent.tools}
        assert tool_names == {"send_message"}
        assert len(root_agent.tools) == 1

    def test_has_sub_agent_client(self):
        from agent_1_adk.agent import root_agent

        assert hasattr(root_agent, "_sub_agent_client")
        assert root_agent._sub_agent_client is not None
