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
from shared.observability import init_langfuse, shutdown_langfuse

setup_file_logging("quant")
init_langfuse(service_name="quant_agent")
atexit.register(shutdown_langfuse)
from opentelemetry.instrumentation.starlette import StarletteInstrumentor

StarletteInstrumentor().instrument()

try:
    from openinference.instrumentation.langchain import LangChainInstrumentor
    LangChainInstrumentor().instrument()
except Exception:
    logger.warning("LangChainInstrumentor unavailable; LangGraph traces will not include LLM spans")

from starlette.responses import JSONResponse
from starlette.routing import Route

from shared.generic_executor import GenericAgentExecutor
from .executor import QuantAgent

logger = logging.getLogger(__name__)


async def health(request):
    # Health check for orchestrator-level monitoring and container orchestration probes
    return JSONResponse({"status": "ok", "agent": "quant"})

host = os.environ.get("HOST", "localhost")

agent_card = AgentCard(
    name="Quant Analysis Agent",
    description="Computes quantitative risk metrics and financial analysis using yfinance and LangGraph",
    version="1.0.0",
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC", url=f"http://{host}:8003/a2a"
        )
    ],
    default_input_modes=["text", "text/plain"],
    default_output_modes=["text", "application/json"],
    skills=[
        AgentSkill(
            id="quant_analysis",
            name="Quantitative Analysis",
            description="Compute Sharpe ratio, Beta, VaR, volatility, DCF valuation, stress tests, and portfolio correlation",
            tags=["quantitative", "risk metrics", "sharpe", "beta", "dcf", "valuation"],
            examples=[
                "Calculate risk metrics for NVDA",
                "Run a DCF valuation on AAPL",
                "Stress test MSFT under different market scenarios",
            ],
        )
    ],
)

request_handler = DefaultRequestHandler(
    agent_executor=GenericAgentExecutor(QuantAgent()),
    task_store=SQLiteTaskStore(),
    agent_card=agent_card,
)

routes = [Route("/health", health)]  # Liveness check
routes.extend(create_agent_card_routes(agent_card))  # A2A agent card discovery
routes.extend(create_jsonrpc_routes(request_handler, "/a2a"))  # JSON-RPC task endpoints

app = Starlette(routes=routes, debug=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
