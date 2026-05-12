import logging
import os
import re
import uuid

import uvicorn
from google.protobuf.json_format import MessageToDict
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

from .graph import QuantAnalysisGraph

logger = logging.getLogger(__name__)


class QuantRequestHandler(DefaultRequestHandlerV2):
    def __init__(self):
        self._graph = QuantAnalysisGraph()

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

        period = meta.get("period", "5y")

        try:
            if not ticker:
                raise ValueError("Could not determine ticker")

            logger.info("Running quant graph for %s (period=%s)", ticker, period)

            result = await self._graph.run(
                ticker=ticker,
                period=period,
                portfolio_holdings=[],
            )

            return Task(
                id=str(uuid.uuid4()),
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                    artifacts=[
                        Artifact(
                            name=f"{ticker}_quant_analysis",
                            parts=[_data_part(result)],
                        )
                    ],
            )

        except Exception as e:
            logger.exception("Quant analysis failed")
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
        name="quant-analysis-agent",
        description="Computes quantitative risk metrics and financial analysis",
        version="1.0.0",
        documentation_url=f"http://{os.environ.get('HOST', 'localhost')}:8003",
        capabilities=AgentCapabilities(streaming=True),
        skills=[
            AgentSkill(
                id="quant_analysis",
                name="Quantitative Analysis",
                description="Compute Sharpe ratio, Beta, VaR, volatility, DCF valuation, stress tests",
                input_modes=["text"],
                output_modes=["data"],
            ),
        ],
    )


def create_app() -> Starlette:
    handler = QuantRequestHandler()
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
    uvicorn.run(app, host="0.0.0.0", port=8003)
