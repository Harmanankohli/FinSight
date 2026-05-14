import pytest
from collections.abc import AsyncIterable

from shared.base_agent import BaseAgent


class MockAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="MockAgent",
            description="A mock agent for testing",
            content_types=["text"],
        )

    async def stream(self, query, context_id, task_id) -> AsyncIterable[dict]:
        yield {
            "response_type": "data",
            "is_task_complete": True,
            "require_user_input": False,
            "content": {"result": f"processed: {query}"},
        }


@pytest.mark.asyncio
async def test_mock_agent_stream():
    agent = MockAgent()
    assert agent.agent_name == "MockAgent"
    assert agent.description == "A mock agent for testing"

    results = []
    async for chunk in agent.stream("test query", "ctx-1", "task-1"):
        results.append(chunk)

    assert len(results) == 1
    assert results[0]["is_task_complete"] is True
    assert results[0]["content"]["result"] == "processed: test query"


@pytest.mark.asyncio
async def test_agent_serialization():
    agent = MockAgent()
    d = agent.model_dump()
    assert d["agent_name"] == "MockAgent"
    assert d["description"] == "A mock agent for testing"
