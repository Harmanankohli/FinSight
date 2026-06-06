import asyncio
import atexit
import logging
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn
from starlette.applications import Starlette

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from shared.a2a_store import SQLiteTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from shared.logging_config import setup_file_logging
from shared.observability import init_langfuse, init_instrumentation, shutdown_langfuse

setup_file_logging("market_context")
init_langfuse(service_name="market_context_agent")
atexit.register(shutdown_langfuse)
init_instrumentation("market_context")

from starlette.responses import JSONResponse
from starlette.routing import Route

from shared.generic_executor import GenericAgentExecutor
from .executor import MarketContextAgent

logger = logging.getLogger(__name__)


async def health(request):
    return JSONResponse({"status": "ok", "agent": "market_context"})


async def release_evals(request):
    from shared.eval_gate import release_evals as _release
    n = await _release()
    return JSONResponse({"released": n})

host = os.environ.get("HOST", "localhost")

agent_card = AgentCard(
    name="Market Context Agent",
    description="Provides macro regime analysis (yield curve, VIX, DXY, sector rotation) and competitive peer landscape positioning using CrewAI",
    version="2.0.0",
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC", url=f"http://{host}:8004/a2a"
        )
    ],
    default_input_modes=["text", "text/plain"],
    default_output_modes=["text", "application/json"],
    skills=[
        AgentSkill(
            id="macro_regime_analysis",
            name="Macro Regime Analysis",
            description="Assess current macro environment (yield curve regime, VIX volatility, DXY dollar strength, sector ETF rotation) and determine if it favours or penalises the target stock",
            tags=["macro", "yield curve", "VIX", "DXY", "sector rotation", "regime"],
            examples=[
                "What is the macro environment for NVDA?",
                "Does the current rate regime favour AAPL?",
            ],
        ),
        AgentSkill(
            id="peer_landscape_analysis",
            name="Competitive Peer Landscape",
            description="Compare target stock against sector peers on growth, margins, and valuation to identify relative positioning headwinds and tailwinds",
            tags=["peers", "competitive", "valuation", "margins", "growth", "sector"],
            examples=[
                "How does MSFT compare to its software peers?",
                "Is TSLA expensive relative to auto sector peers?",
            ],
        ),
    ],
)

request_handler = DefaultRequestHandler(
    agent_executor=GenericAgentExecutor(MarketContextAgent()),
    task_store=SQLiteTaskStore(),
    agent_card=agent_card,
)

routes = [Route("/health", health), Route("/release-evals", release_evals, methods=["POST"])]
routes.extend(create_agent_card_routes(agent_card))  # A2A agent card discovery
routes.extend(create_jsonrpc_routes(request_handler, "/a2a"))  # JSON-RPC task endpoints

app = Starlette(routes=routes, debug=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8004)
