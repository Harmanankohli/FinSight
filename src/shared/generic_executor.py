"""Reusable A2A AgentExecutor that delegates execution to a BaseAgent.

Translates the agent's stream() async generator into A2A protocol events
(TaskStatusUpdateEvent / TaskArtifactUpdateEvent) so any BaseAgent subclass
can participate in A2A task lifecycle without writing A2A boilerplate.
"""
import asyncio
import logging

# ── A2A protocol types and helpers ──────────────────────────────────────
from a2a.helpers import new_task_from_user_message, new_text_message
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
# ── Protobuf helpers for structured data ────────────────────────────────
from google.protobuf.struct_pb2 import Struct, Value

# ── Project internals ───────────────────────────────────────────────────
from shared.base_agent import BaseAgent
from shared.logging_config import logged
from shared.trace_context import (
    current_session_id,
    current_trace_id,
    current_user_id,
    extract_trace_ids,
    extract_user_id,
)

logger = logging.getLogger(__name__)


class GenericAgentExecutor(AgentExecutor):
    """Reusable A2A AgentExecutor that delegates all logic to a BaseAgent.

    Each yielded dict from ``agent.stream()`` is mapped to one of three
    A2A event branches — complete, input-required, or working progress.
    Errors and cancellations are translated to their corresponding A2A
    task states.
    """

    def __init__(self, agent: BaseAgent):
        """Wrap *agent* so it can be driven through the A2A lifecycle."""
        self.agent = agent
        self._task: asyncio.Task[None] | None = None

    @logged(log_args=False, log_result=False)  # type: ignore[untyped-decorator] -- logged() returns Any
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Run the agent and emit A2A events over the event queue.

        Event mapping from ``agent.stream()`` yielded dicts::

            is_task_complete + is_error ──► TASK_STATE_FAILED / COMPLETED
            require_user_input        ──► TASK_STATE_INPUT_REQUIRED
            neither (progress tick)   ──► TASK_STATE_WORKING
            CancelledError            ──► TASK_STATE_CANCELED

        Trace/session IDs present in the query string are propagated into
        ``ContextVar``\ s so downstream log lines carry them automatically.
        """
        # ── Set up: grab the current asyncio task (for cancel()), resolve the
        #    A2A task object (create one from the request message if needed). ──
        self._task = asyncio.current_task()
        query = context.get_user_input()
        task = context.current_task
        if not task:
            if context.message is None:
                raise ValueError("No task or message in RequestContext")
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        # ── Populate ContextVars so async log / trace calls inside execute()
        #    carry trace/session/user IDs without explicit plumbing. ──
        trace_id_val, _, _ = extract_trace_ids(query)
        if trace_id_val:
            current_trace_id.set(trace_id_val)
        user_id = extract_user_id(query)
        if user_id:
            current_user_id.set(user_id)
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
            #    three branches — complete / input-required / working progress. ──
            async for item in self.agent.stream(query, task.context_id, task.id):
                is_task_complete = item.get("is_task_complete", False)
                require_user_input = item.get("require_user_input", False)

                # ── Branch 1: final result (success or error) ──
                if is_task_complete:
                    is_error = item.get("is_error", False)
                    content = item.get("content", {})
                    # Structured data (dict) → protobuf Struct so downstream
                    # consumers receive typed fields rather than raw text.
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
                                state=TaskState.TASK_STATE_FAILED
                                if is_error
                                else TaskState.TASK_STATE_COMPLETED,
                                message=new_text_message(str(content)) if is_error else None,
                            ),
                        )
                    )
                    return

                # ── Branch 2: agent needs user input before continuing ──
                if require_user_input:
                    await event_queue.enqueue_event(
                        TaskStatusUpdateEvent(
                            task_id=task.id,
                            context_id=task.context_id,
                            status=TaskStatus(
                                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                                message=new_text_message(str(item.get("content", ""))),
                            ),
                        )
                    )
                    return

                # ── Branch 3: intermediate progress tick (streaming partial text) ──
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
            # Emit a terminal CANCELED event — the caller is responsible for
            # cleaning up the task in whatever store they use.
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task.id,
                    context_id=task.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
                )
            )
            raise

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancel the in-flight agent task if one is running.

        Called by the A2A server when a client requests task cancellation.
        """
        if self._task and not self._task.done():
            self._task.cancel()
