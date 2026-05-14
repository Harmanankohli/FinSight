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
        from shared.config import MCP_TIMEOUT
        self._mcp = MCPClient(config_path="agent_4_crewai/mcp_config.yaml", timeout=MCP_TIMEOUT)
        self._wrapper = MCPClientWrapper(self._mcp)
        self._connected = False

    async def _connect(self):
        if not self._connected:
            await self._mcp.connect_all()
            self._connected = True

    async def _collect_data_parallel(self, ticker: str) -> dict:
        """Call all MCP tools in parallel and return aggregated data."""
        results = {}

        async def call(tool, args):
            try:
                r = await self._mcp.call_tool_by_name(tool, args)
                if hasattr(r, "content"):
                    for item in r.content:
                        txt = item.text if hasattr(item, "text") else str(item)
                        import json
                        try:
                            return json.loads(txt)
                        except (json.JSONDecodeError, TypeError):
                            return {"raw": txt[:500]}
                return {"raw": str(r)[:500]}
            except Exception as e:
                return {"error": str(e)}

        import asyncio, json
        await self._connect()

        tasks = {
            "news": call("get_news_sentiment", {"ticker": ticker, "limit": 5}),
            "filings": call("get_company_filings", {"ticker": ticker, "form_types": "", "limit": 3}),
        }
        done = await asyncio.gather(*[asyncio.create_task(t) for t in tasks.values()], return_exceptions=True)
        for name, result in zip(tasks.keys(), done):
            if isinstance(result, Exception):
                results[name] = {"error": str(result)}
            else:
                results[name] = result
        return results

    async def analyze(self, ticker: str, query_text: str) -> dict:
        data = await self._collect_data_parallel(ticker)
        logger.info("Collected data for %s: %s", ticker, list(data.keys()))
        crew_builder = SentimentIntelligenceCrew(self._wrapper)
        return await crew_builder.analyze(ticker, precollected_data=data)


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
