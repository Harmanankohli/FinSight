import logging

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
from google.protobuf.struct_pb2 import Struct, Value

from shared.base_agent import BaseAgent

logger = logging.getLogger(__name__)


class GenericAgentExecutor(AgentExecutor):
    """Reusable A2A AgentExecutor that delegates all logic to a BaseAgent."""

    def __init__(self, agent: BaseAgent):
        self.agent = agent

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        logger.info("Executing agent %s", self.agent.agent_name)

        query = context.get_user_input()
        task = context.current_task
        if not task:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

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

        async for item in self.agent.stream(query, task.context_id, task.id):
            is_task_complete = item.get("is_task_complete", False)
            require_user_input = item.get("require_user_input", False)

            if is_task_complete:
                content = item.get("content", {})
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
                        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                    )
                )
                return

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

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        raise Exception("cancel not supported")
