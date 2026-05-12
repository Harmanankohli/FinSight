import logging
import os
import re
import uuid

import uvicorn
from starlette.applications import Starlette

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers.default_request_handler_v2 import (
    DefaultRequestHandlerV2,
)
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.types import (
    AgentCapabilities,
    AgentCard,
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

from shared.mcp_client import MCPClient

from .crew import SentimentIntelligenceCrew
from .mcp_tools import MCPClientWrapper

logger = logging.getLogger(__name__)


from google.protobuf.struct_pb2 import Struct as StructProto, Value as ValueProto


def _meta_dict(metadata) -> dict:
    if metadata is None:
        return {}
    if isinstance(metadata, dict):
        return metadata
    try:
        return MessageToDict(metadata)
    except Exception:
        return {}


def _data_part(data: dict) -> Part:
    s = StructProto()
    s.update(data)
    return Part(data=ValueProto(struct_value=s))


class SentimentRequestHandler(DefaultRequestHandlerV2):
    def __init__(self):
        self._mcp = MCPClient(config_path="agent_4_crewai/mcp_config.yaml")
        self._wrapper = MCPClientWrapper(self._mcp)
        self._crew = SentimentIntelligenceCrew(self._wrapper)
        self._discovered_tools: list | None = None

    async def _get_tools(self) -> list:
        if self._discovered_tools is None:
            try:
                await self._mcp.connect_all()
                self._discovered_tools = await self._wrapper.discover_tools()
                logger.info("Discovered %d MCP tools", len(self._discovered_tools))
            except Exception as e:
                logger.warning("MCP discovery failed: %s", e)
                self._discovered_tools = []
        return self._discovered_tools or []

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
                raise ValueError("Could not determine ticker")

            tools = await self._get_tools()
            result = await self._crew.analyze(ticker.upper(), tools=tools)

            return Task(
                id=str(uuid.uuid4()),
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                artifacts=[
                    Artifact(
                        name=f"{ticker}_sentiment",
                        parts=[_data_part(result)],
                    )
                ],
            )

        except Exception as e:
            logger.exception("Sentiment analysis failed")
            return Task(
                id=str(uuid.uuid4()),
                status=TaskStatus(state=TaskState.TASK_STATE_FAILED),
                artifacts=[
                    Artifact(
                        name="error", parts=[Part(text=str(e))]
                    )
                ],
            )

    async def on_get_task(self, params, context) -> Task | None:
        return None

    async def on_cancel_task(self, params, context) -> Task | None:
        return None

    async def on_list_tasks(self, params, context) -> ListTasksResponse:
        return ListTasksResponse(tasks=[])

    async def on_subscribe_to_task(self, params, context):
        return
        yield

    async def on_message_send_stream(self, params, context):
        return
        yield

    async def on_get_extended_agent_card(self, params, context) -> AgentCard:
        return _build_agent_card()

    async def on_get_task_push_notification_config(self, params, context):
        raise NotImplementedError

    async def on_create_task_push_notification_config(self, params, context):
        raise NotImplementedError

    async def on_delete_task_push_notification_config(self, params, context):
        pass

    async def on_list_task_push_notification_configs(self, params, context):
        return ListTaskPushNotificationConfigsResponse(configs=[])


def _build_agent_card() -> AgentCard:
    return AgentCard(
        name="sentiment-intelligence-agent",
        description="Synthesizes social media sentiment, analyst commentary, and insider trading signals",
        version="1.0.0",
        documentation_url=f"http://{os.environ.get('HOST', 'localhost')}:8004",
        capabilities=AgentCapabilities(streaming=True),
        skills=[
            AgentSkill(
                id="sentiment_analysis",
                name="Sentiment & Narrative Intelligence",
                description=(
                    "Analyze Reddit/Twitter sentiment, aggregate analyst ratings, "
                    "monitor insider trading, produce investment narrative"
                ),
                input_modes=["text"],
                output_modes=["data"],
            ),
        ],
    )


def create_app() -> Starlette:
    handler = SentimentRequestHandler()
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
    uvicorn.run(app, host="0.0.0.0", port=8004)
