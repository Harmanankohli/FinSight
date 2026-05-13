import json
import logging

from a2a.helpers import new_task_from_user_message, new_text_artifact, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import TaskArtifactUpdateEvent, TaskState, TaskStatus, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct, Value

from shared.mcp_client import MCPClient

from .document_ingestion import DocumentIngestionPipeline
from .index_manager import FinancialIndexManager

logger = logging.getLogger(__name__)


class RAGAgent:
    def __init__(self):
        self.index = FinancialIndexManager()
        self._mcp: MCPClient | None = None
        self._ingestion: DocumentIngestionPipeline | None = None

    async def _ensure_ingested(self, ticker: str) -> None:
        if self._mcp is None:
            self._mcp = MCPClient(config_path="agent_2_llamaindex/mcp_config.yaml")
            try:
                await self._mcp.connect_all()
            except Exception as e:
                logger.warning("MCP connect failed (non-fatal): %s", e)
                self._mcp = None
                return
            self._ingestion = DocumentIngestionPipeline(self.index)
        try:
            result = await self._mcp.call_tool_by_name(
                "get_company_filings",
                {"ticker": ticker, "form_types": ["10-K", "10-Q", "8-K"], "limit": 5},
            )
            if hasattr(result, "content"):
                for item in result.content:
                    raw = item.text if hasattr(item, "text") else str(item)
                    filings = json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(filings, dict):
                        self._ingestion.ingest_sec_filings_batch(
                            ticker, filings.get("filings", [])
                        )
        except Exception as e:
            logger.warning("Auto-ingest failed for %s: %s", ticker, e)

    async def query(self, ticker: str, query_text: str) -> dict:
        await self._ensure_ingested(ticker)
        return await self.index.query(ticker, query_text)


class RAGAgentExecutor(AgentExecutor):
    def __init__(self):
        self.agent = RAGAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        await event_queue.enqueue_event(task)

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_WORKING,
                    message=new_text_message("Retrieving documents..."),
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
            import re
            match = re.search(r"\b[A-Z]{1,5}\b", query_text)
            ticker = match.group(0) if match else ""

        try:
            result = await self.agent.query(ticker, query_text)
            from a2a.types import Artifact, Part as PartProto
            s = Struct()
            s.update(result)
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    artifact=Artifact(
                        name=f"{ticker}_rag_result",
                        parts=[PartProto(data=Value(struct_value=s))],
                    ),
                )
            )
        except Exception as e:
            logger.exception("RAG query failed")
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    artifact=new_text_artifact(name="error", text=str(e)),
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
