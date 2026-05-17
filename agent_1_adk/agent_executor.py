import logging

from a2a.helpers import (
    new_task_from_user_message,
    new_text_message,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from google.adk.runners import Runner
from google.genai import types
from langfuse import propagate_attributes
from shared.observability import get_langfuse_client

from .agent import root_agent as _root_agent

logger = logging.getLogger(__name__)


class FinSightAgentExecutor(AgentExecutor):
    """Executor that runs the ADK orchestrator agent directly.

    The ADK agent (LlmAgent) has two tools:
    - ``list_remote_agents``: discover available sub-agents
    - ``send_message``: delegate tasks to sub-agents via A2A

    No pre-fetching. The LLM decides when to call tools.
    """

    def __init__(self, runner: Runner) -> None:
        self._runner = runner

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        context_id = context.context_id
        user_id = _resolve_user_id(context)

        await self.ensure_session(user_id, context_id)

        task = context.current_task
        if not task:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.update_status(
            TaskState.TASK_STATE_WORKING,
            new_text_message("Processing your request..."),
        )

        content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=context.get_user_input())],
        )

        langfuse = get_langfuse_client()
        user_input = context.get_user_input()
        with langfuse.start_as_current_observation(
            as_type="span",
            name="orchestrator-execute",
            input=user_input,
        ) as span:
            with propagate_attributes(session_id=context_id, user_id=user_id):
                try:
                    async for event in self._runner.run_async(
                        user_id=user_id,
                        session_id=context_id,
                        new_message=content,
                    ):
                        if event.is_final_response():
                            await self._process_response(event, updater, task, span)
                except Exception:
                    logger.exception("Error during agent execution")
                    span.update(output={"error": "Agent execution failed"})
                await updater.update_status(
                    TaskState.TASK_STATE_FAILED,
                    new_text_message(
                        "An exception occurred while performing the operation"
                    ),
                )

    async def _process_response(
        self, event, updater: TaskUpdater, task, span=None
    ) -> None:
        if not (
            event.content
            and event.content.parts
            and event.content.parts[0].text
        ):
            if span:
                span.update(output={"error": "No text content"})
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                new_text_message("[No text content]"),
            )
            return

        text = event.content.parts[0].text.strip()
        if span:
            span.update(output={"response": text[:2000]})
        await updater.update_status(
            TaskState.TASK_STATE_COMPLETED,
            new_text_message(text, task.context_id, task.id),
            final=True,
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
