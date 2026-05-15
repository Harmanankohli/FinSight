import asyncio
import json
import logging
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, TaskState, TextPart
from a2a.utils import new_agent_text_message, new_task
from google.adk.runners import Runner
from google.genai import types

from .agent import _agent_list, _client

logger = logging.getLogger(__name__)


class FinSightAgentExecutor(AgentExecutor):
    """Executor that pre-fetches sub-agent data before LLM inference."""

    def __init__(self, runner: Runner) -> None:
        self._runner = runner

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        context_id = context.context_id
        user_id = _resolve_user_id(context)
        query = context.get_user_input()

        # 1. Call all sub-agents in parallel before LLM
        research = await self._collect_research(query)

        # 2. Build enriched message with research data
        enriched = f"User query: {query}\n\nResearch data:\n{research}"
        content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=enriched)],
        )

        await self.ensure_session(user_id, context_id)

        task = context.current_task
        if not task:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)

        # 3. Run LLM with enriched context
        try:
            async for event in self._runner.run_async(
                user_id=user_id,
                session_id=context_id,
                new_message=content,
            ):
                if event.is_final_response():
                    await self._process_response(
                        event, updater, task
                    )
        except Exception:
            logger.exception("Error during agent execution")
            await self._update_task_status(
                updater,
                TaskState.failed,
                "An exception occurred while performing the operation",
                task,
            )

    async def _collect_research(self, query: str) -> str:
        names = [a["name"] for a in _agent_list]
        if not names:
            return "No agents available."

        async def _call(name: str) -> tuple[str, Any]:
            try:
                text = await _client.send_message(
                    name, query
                )
                return name, text
            except Exception as e:
                logger.warning(
                    "Agent '%s' failed: %s", name, e
                )
                return name, json.dumps({"error": str(e)})

        results = await asyncio.gather(
            *[_call(n) for n in names]
        )
        lines = []
        for name, text in results:
            try:
                parsed = json.loads(text)
                lines.append(
                    f"--- {name} ---\n{json.dumps(parsed, indent=2)}"
                )
            except (json.JSONDecodeError, TypeError):
                lines.append(f"--- {name} ---\n{text}")
        return "\n\n".join(lines)

    async def _process_response(
        self, event, updater, task
    ) -> None:
        if not (
            event.content
            and event.content.parts
            and event.content.parts[0].text
        ):
            await self._update_task_status(
                updater,
                TaskState.failed,
                "[No text content]",
                task,
            )
            return

        text = event.content.parts[0].text.strip()
        await self._update_task_status(
            updater, TaskState.completed, text, task, final=True
        )

    async def _update_task_status(
        self,
        updater: TaskUpdater,
        state: TaskState,
        message_text: str,
        task,
        metadata: dict | None = None,
        final: bool = False,
    ) -> None:
        await updater.update_status(
            state,
            new_agent_text_message(
                message_text, task.context_id, task.id
            ),
            metadata=metadata,
            final=final,
        )

    async def ensure_session(
        self, user_id: str, context_id: str
    ) -> None:
        session = await self._runner.session_service.get_session(
            app_name=self._runner.app_name,
            user_id=user_id,
            session_id=context_id,
        )
        if session is None:
            await self._runner.session_service.create_session(
                app_name=self._runner.app_name,
                user_id=user_id,
                session_id=context_id,
            )

    async def cancel(self, context, event_queue) -> None:
        raise NotImplementedError("Cancellation is not supported")


def _resolve_user_id(context: RequestContext) -> str:
    if (
        context.call_context
        and context.call_context.user.is_authenticated
    ):
        return context.call_context.user.user_name
    return "a2a_user"
