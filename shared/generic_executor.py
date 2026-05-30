import asyncio
import logging

from a2a.helpers import new_task_from_user_message, new_text_message
from shared.trace_context import extract_trace_ids, current_trace_id, current_session_id
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    Artifact,
    Part,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from google.protobuf.struct_pb2 import Struct, Value

from shared.base_agent import BaseAgent

logger = logging.getLogger(__name__)


class GenericAgentExecutor(AgentExecutor):
    """Reusable A2A AgentExecutor that delegates all logic to a BaseAgent.

    Wraps the agent's ``stream()`` async generator into the A2A protocol
    (TaskStatusUpdateEvent / TaskArtifactUpdateEvent) so any BaseAgent
    subclass can participate in the A2A task lifecycle without writing
    A2A boilerplate.
    """

    def __init__(self, agent: BaseAgent):
        self.agent = agent
        self._task: asyncio.Task | None = None

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Run the agent and emit A2A events over the event queue.

        Translates the agent's stream() yielded dicts into:
          - TASK_STATE_WORKING       — streaming progress
          - TASK_STATE_COMPLETED      — final result with artifact
          - TASK_STATE_FAILED         — error result with artifact
          - input_required            — agent needs user input
          - TASK_STATE_CANCELED       — task was cancelled via cancel()
        """
        self._task = asyncio.current_task()
        query = context.get_user_input()
        task = context.current_task
        if not task:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        # Populate ContextVars so log lines inside execute() carry the trace/session IDs.
        trace_id_val, _, _ = extract_trace_ids(query)
        if trace_id_val:
            current_trace_id.set(trace_id_val)
        if task and task.context_id:
            current_session_id.set(task.context_id)

        logger.info("Executing agent %s", self.agent.agent_name)

        try:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task.id,
                    context_id=task.context_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_WORKING,
                        message=new_text_message(f"Running {self.agent.agent_name}..."),
                    ),
                )
            )

            # ── Stream event loop: each yielded dict from the agent maps to one of
            #    three branches (complete / input-required / working progress). ──
            async for item in self.agent.stream(query, task.context_id, task.id):
                is_task_complete = item.get("is_task_complete", False)
                require_user_input = item.get("require_user_input", False)

                # ── Branch 1: final result (success or error) ──
                if is_task_complete:
                    is_error = item.get("is_error", False)
                    content = item.get("content", {})
                    # Structured data (dict) → protobuf Struct so downstream A2A
                    # consumers receive typed fields rather than a raw text blob.
                    if item.get("response_type") == "data" and isinstance(content, dict):
                        s = Struct()
                        s.update(content)
                        part = Part(data=Value(struct_value=s))
                    else:
                        part = Part(text=str(content))

                    await event_queue.enqueue_event(
                        TaskArtifactUpdateEvent(
                            task_id=task.id,
                            context_id=task.context_id,
                            artifact=Artifact(
                                name=f"{self.agent.agent_name}-result",
                                parts=[part],
                            ),
                        )
                    )

                    await event_queue.enqueue_event(
                        TaskStatusUpdateEvent(
                            task_id=task.id,
                            context_id=task.context_id,
                            status=TaskStatus(
                                state=TaskState.TASK_STATE_FAILED if is_error else TaskState.TASK_STATE_COMPLETED,
                                message=new_text_message(str(content)) if is_error else None,
                            ),
                        )
                    )
                    return

                # ── Branch 2: agent requests user input before continuing ──
                if require_user_input:
                    await event_queue.enqueue_event(
                        TaskStatusUpdateEvent(
                            task_id=task.id,
                            context_id=task.context_id,
                            status=TaskStatus(
                                state=TaskState.input_required,
                                message=new_text_message(str(item.get("content", ""))),
                            ),
                        )
                    )
                    return

                # ── Branch 3: intermediate progress (streaming partial text) ──
                await event_queue.enqueue_event(
                    TaskStatusUpdateEvent(
                        task_id=task.id,
                        context_id=task.context_id,
                        status=TaskStatus(
                            state=TaskState.TASK_STATE_WORKING,
                            message=new_text_message(str(item.get("content", ""))),
                        ),
                    )
                )

        except asyncio.CancelledError:
            logger.info("Agent %s task cancelled", self.agent.agent_name)
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task.id,
                    context_id=task.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
                )
            )
            raise

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Cancel the in-flight agent task if one is running."""
        if self._task and not self._task.done():
            self._task.cancel()
