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

from shared.mcp_client import MCPClient

from .crew import SentimentIntelligenceCrew
from .mcp_tools import MCPClientWrapper

logger = logging.getLogger(__name__)


class SentimentAgent:
    def __init__(self):
        import os as _os
        from shared.config import GROQ_API_KEY, LLM_BASE_URL, MCP_TIMEOUT
        _os.environ.setdefault("VLLM_API_KEY", GROQ_API_KEY)
        _os.environ.setdefault("VLLM_BASE_URL", LLM_BASE_URL)
        _os.environ.setdefault("OPENAI_API_KEY", GROQ_API_KEY)
        _os.environ.setdefault("OPENAI_BASE_URL", LLM_BASE_URL)
        self._mcp = MCPClient(config_path="agent_4_crewai/mcp_config.yaml", timeout=MCP_TIMEOUT)
        self._wrapper = MCPClientWrapper(self._mcp)
        self._tools_discovered = False
        self._tools: list = []

    async def _ensure_tools(self) -> list:
        if not self._tools_discovered:
            try:
                await self._mcp.connect_all()
                self._tools = await self._wrapper.discover_tools()
                self._tools_discovered = True
                logger.info("Discovered %d MCP tools", len(self._tools))
            except Exception as e:
                logger.warning("MCP discovery failed: %s", e)
                self._tools = []
                self._tools_discovered = True
        return self._tools

    async def analyze(self, ticker: str, query_text: str) -> dict:
        tools = await self._ensure_tools()
        crew_builder = SentimentIntelligenceCrew(self._wrapper)
        return await crew_builder.analyze(ticker, tools=tools)


class SentimentAgentExecutor(AgentExecutor):
    def __init__(self):
        self.agent = SentimentAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        await event_queue.enqueue_event(task)

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_WORKING,
                    message=new_text_message("Gathering sentiment intelligence..."),
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

        try:
            result = await self.agent.analyze(ticker, query_text)
            s = Struct()
            s.update(result)
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    artifact=Artifact(
                        name=f"{ticker}_sentiment",
                        parts=[PartProto(data=Value(struct_value=s))],
                    ),
                )
            )
        except Exception as e:
            logger.exception("Sentiment analysis failed")
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
