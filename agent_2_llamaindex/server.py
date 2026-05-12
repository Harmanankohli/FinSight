import logging
import os
import re
import uuid

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers.default_request_handler_v2 import (
    DefaultRequestHandlerV2,
)
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Artifact,
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetExtendedAgentCardRequest,
    GetTaskRequest,
    GetTaskPushNotificationConfigRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    Part,
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskPushNotificationConfig,
    TaskState,
    TaskStatus,
)

from google.protobuf.json_format import MessageToDict

from .index_manager import FinancialIndexManager

logger = logging.getLogger(__name__)


def _meta_dict(metadata) -> dict:
    if metadata is None:
        return {}
    if isinstance(metadata, dict):
        return metadata
    try:
        return MessageToDict(metadata)
    except Exception:
        return {}


from google.protobuf.struct_pb2 import Struct as StructProto, Value as ValueProto


def _data_part(data: dict) -> Part:
    s = StructProto()
    s.update(data)
    return Part(data=ValueProto(struct_value=s))


from shared.mcp_client import MCPClient

from .document_ingestion import DocumentIngestionPipeline


class RAGRequestHandler(DefaultRequestHandlerV2):
    def __init__(self, index_manager: FinancialIndexManager):
        self._index = index_manager
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
            self._ingestion = DocumentIngestionPipeline(self._index)

        try:
            result = await self._mcp.call_tool_by_name(
                "get_company_filings",
                {"ticker": ticker, "form_types": ["10-K", "10-Q", "8-K"], "limit": 5},
            )
            if hasattr(result, "content"):
                import json as _json
                for item in result.content:
                    raw = item.text if hasattr(item, "text") else str(item)
                    filings = _json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(filings, dict):
                        self._ingestion.ingest_sec_filings_batch(
                            ticker, filings.get("filings", [])
                        )
        except Exception as e:
            logger.warning("Auto-ingest failed for %s: %s", ticker, e)

    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Message | Task:
        parts = params.message.parts or []
        meta = _meta_dict(params.metadata)
        query_text = ""
        for p in parts:
            if hasattr(p, "text") and p.text:
                query_text = p.text
                break

        ticker = meta.get("ticker", "")
        if not ticker:
            match = re.search(r"\b[A-Z]{1,5}\b", query_text)
            ticker = match.group(0) if match else ""

        try:
            if not ticker:
                raise ValueError("Could not determine ticker from query")

            await self._ensure_ingested(ticker)

            filing_types = meta.get("filing_types", [])
            if filing_types and any("10-" in ft for ft in filing_types):
                result = await self._index.query_sec_filings(ticker, query_text)
            elif filing_types and any("earnings" in ft.lower() for ft in filing_types):
                result = await self._index.query_earnings(ticker, query_text)
            else:
                result = await self._index.query(ticker, query_text)

            task = Task(
                id=str(uuid.uuid4()),
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                artifacts=[
                    Artifact(
                        name=f"{ticker}_rag_result",
                        parts=[_data_part(result)],
                    )
                ],
            )
            return task

        except Exception as e:
            logger.exception("RAG query failed")
            return Task(
                id=str(uuid.uuid4()),
                status=TaskStatus(state=TaskState.TASK_STATE_FAILED),
                artifacts=[
                    Artifact(
                        name="error",
                        parts=[Part(text=str(e))],
                    )
                ],
            )

    async def on_get_task(
        self, params: GetTaskRequest, context: ServerCallContext
    ) -> Task | None:
        return None

    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        return None

    async def on_list_tasks(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        return ListTasksResponse(tasks=[])

    async def on_subscribe_to_task(
        self, params: SubscribeToTaskRequest, context: ServerCallContext
    ):
        return
        yield

    async def on_message_send_stream(self, params, context):
        return
        yield

    async def on_get_extended_agent_card(
        self, params: GetExtendedAgentCardRequest, context: ServerCallContext
    ) -> AgentCard:
        return _build_agent_card()

    async def on_get_task_push_notification_config(
        self, params: GetTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> TaskPushNotificationConfig:
        raise NotImplementedError

    async def on_create_task_push_notification_config(
        self, params: TaskPushNotificationConfig, context: ServerCallContext
    ) -> TaskPushNotificationConfig:
        raise NotImplementedError

    async def on_delete_task_push_notification_config(
        self, params: DeleteTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> None:
        pass

    async def on_list_task_push_notification_configs(
        self,
        params: ListTaskPushNotificationConfigsRequest,
        context: ServerCallContext,
    ) -> ListTaskPushNotificationConfigsResponse:
        return ListTaskPushNotificationConfigsResponse(configs=[])


def _build_agent_card() -> AgentCard:
    return AgentCard(
        name="financial-rag-agent",
        description="Retrieves and analyzes financial documents using RAG",
        version="1.0.0",
        documentation_url=f"http://{os.environ.get('HOST', 'localhost')}:8002",
        capabilities=AgentCapabilities(streaming=True),
        skills=[
            AgentSkill(
                id="sec_filing_retrieval",
                name="SEC Filing Retrieval",
                description="Retrieves and analyzes SEC 10-K, 10-Q, 8-K filings",
                input_modes=["text"],
                output_modes=["text", "data"],
            ),
            AgentSkill(
                id="earnings_summary",
                name="Earnings Summary",
                description="Summarizes earnings call transcripts",
                input_modes=["text"],
                output_modes=["text"],
            ),
        ],
    )


def create_app() -> Starlette:
    index_manager = FinancialIndexManager()
    handler = RAGRequestHandler(index_manager)

    routes = []
    routes.extend(create_agent_card_routes(_build_agent_card()))
    routes.extend(
        create_jsonrpc_routes(
            handler,
            rpc_url="/a2a",
            context_builder=DefaultServerCallContextBuilder(),
        )
    )

    return Starlette(routes=routes, debug=True)


app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8002)
