"""In-process A2A protocol test — WORKING→artifact→COMPLETED lifecycle.

WP 3.1: verify GenericAgentExecutor and A2A event flow.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.auth

try:
    from a2a.types import TaskState
    HAS_A2A = True
except Exception:
    HAS_A2A = False


class _FakeEventQueue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class _FakeTask:
    def __init__(self):
        self.id = str(uuid.uuid4())
        self.context_id = f"ctx-{uuid.uuid4().hex[:8]}"


class _FakeCallContext:
    def __init__(self):
        self.user = MagicMock()
        self.user.is_authenticated = False
        self.user.user_name = "test"


class _FakeRequestContext:
    def __init__(self, message_text: str = "Analyze NVDA"):
        self.message = MagicMock()
        self.message.parts = [MagicMock()]
        self.message.parts[0].text = message_text
        self.message.parts[0].data = None
        self._task = _FakeTask()
        self.context_id = self._task.context_id
        self.call_context = _FakeCallContext()

    def get_user_input(self):
        return self.message.parts[0].text

    @property
    def current_task(self):
        return self._task


class _FakeAgent:
    agent_name = "test-agent"

    def __init__(self, steps=None):
        self.steps = steps or [
            {"is_task_complete": False, "content": "Working..."},
            {"is_task_complete": True, "content": {"result": "success", "value": 42}, "response_type": "data"},
        ]

    async def stream(self, query: str, context_id: str, task_id: str):
        for step in self.steps:
            yield step


@pytest.mark.skipif(not HAS_A2A, reason="a2a-sdk protobuf not available")
class TestGenericAgentExecutor:
    def _state_name(self, event):
        return TaskState.Name(event.status.state)

    @pytest.mark.asyncio
    async def test_working_completed_lifecycle(self):
        from shared.generic_executor import GenericAgentExecutor

        executor = GenericAgentExecutor(_FakeAgent())
        ctx = _FakeRequestContext()
        eq = _FakeEventQueue()

        await executor.execute(ctx, eq)

        state_names = [self._state_name(e) for e in eq.events if hasattr(e, "status")]
        assert "TASK_STATE_WORKING" in state_names
        assert "TASK_STATE_COMPLETED" in state_names
        artifacts = [e for e in eq.events if hasattr(e, "artifact")]
        assert len(artifacts) >= 1

    @pytest.mark.asyncio
    async def test_failure_lifecycle(self):
        from shared.generic_executor import GenericAgentExecutor

        executor = GenericAgentExecutor(_FakeAgent(steps=[
            {"is_task_complete": False, "content": "Running..."},
            {"is_task_complete": True, "is_error": True, "content": "Something broke"},
        ]))
        ctx = _FakeRequestContext()
        eq = _FakeEventQueue()

        await executor.execute(ctx, eq)

        state_names = [self._state_name(e) for e in eq.events if hasattr(e, "status")]
        assert "TASK_STATE_FAILED" in state_names

    @pytest.mark.asyncio
    async def test_cancel_lifecycle(self):
        from shared.generic_executor import GenericAgentExecutor

        async def _slow_stream(query, ctx_id, task_id):
            yield {"is_task_complete": False, "content": "Starting..."}
            await asyncio.sleep(10)
            yield {"is_task_complete": True, "content": "Done"}

        slow_agent = MagicMock()
        slow_agent.agent_name = "slow-agent"
        slow_agent.stream = _slow_stream

        executor = GenericAgentExecutor(slow_agent)
        ctx = _FakeRequestContext()
        eq = _FakeEventQueue()

        execute_task = asyncio.create_task(executor.execute(ctx, eq))
        await asyncio.sleep(0.1)
        await executor.cancel(ctx, eq)

        with pytest.raises(asyncio.CancelledError):
            await execute_task

    @pytest.mark.asyncio
    async def test_input_required_lifecycle(self):
        from shared.generic_executor import GenericAgentExecutor

        executor = GenericAgentExecutor(_FakeAgent(steps=[
            {"is_task_complete": False, "require_user_input": True, "content": "What is your risk tolerance?"},
        ]))
        ctx = _FakeRequestContext()
        eq = _FakeEventQueue()

        await executor.execute(ctx, eq)

        state_names = [self._state_name(e) for e in eq.events if hasattr(e, "status")]
        assert "TASK_STATE_INPUT_REQUIRED" in state_names

    @pytest.mark.asyncio
    async def test_structured_data_artifact(self):
        from shared.generic_executor import GenericAgentExecutor

        executor = GenericAgentExecutor(_FakeAgent(steps=[
            {"is_task_complete": True, "content": {"sharpe": 1.5, "vol": 0.25}, "response_type": "data"},
        ]))
        ctx = _FakeRequestContext()
        eq = _FakeEventQueue()

        await executor.execute(ctx, eq)

        artifacts = [e for e in eq.events if hasattr(e, "artifact")]
        assert len(artifacts) >= 1
        artifact = artifacts[0].artifact
        assert artifact.parts[0].data is not None
