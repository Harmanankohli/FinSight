import logging
import re

from a2a.helpers import new_task_from_user_message, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    Artifact,
    Part as PartProto,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct, Value

from .graph import QuantAnalysisGraph

logger = logging.getLogger(__name__)


class QuantAgent:
    def __init__(self):
        self.graph = QuantAnalysisGraph()

    async def analyze(self, ticker: str, period: str = "5y") -> dict:
        return await self.graph.run(ticker, period=period)


class QuantAgentExecutor(AgentExecutor):
    def __init__(self):
        self.agent = QuantAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        await event_queue.enqueue_event(task)

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_WORKING,
                    message=new_text_message("Computing quantitative metrics..."),
                ),
            )
        )

        query_text = ""
        for part in context.message.parts:
            if part.text:
                query_text = part.text
                break

        meta = {}
        if context.message.metadata:
            meta = MessageToDict(context.message.metadata)

        ticker = meta.get("ticker", "")
        if not ticker:
            match = re.search(r"\b[A-Z]{1,5}\b", query_text)
            ticker = match.group(0) if match else ""

        period = meta.get("period", "5y")

        try:
            result = await self.agent.analyze(ticker, period)
            s = Struct()
            s.update(result)
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    artifact=Artifact(
                        name=f"{ticker}_quant_analysis",
                        parts=[PartProto(data=Value(struct_value=s))],
                    ),
                )
            )
        except Exception as e:
            logger.exception("Quant analysis failed")
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    artifact=Artifact(
                        name="error",
                        parts=[PartProto(text=str(e))],
                    ),
                )
            )

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception("cancel not supported")
