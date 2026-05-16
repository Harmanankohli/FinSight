import asyncio
import logging
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn
from starlette.applications import Starlette

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from shared.generic_executor import GenericAgentExecutor
from .executor import QuantAgent

logger = logging.getLogger(__name__)

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
    task_store=InMemoryTaskStore(),
    agent_card=agent_card,
)

routes = []
routes.extend(create_agent_card_routes(agent_card))
routes.extend(create_jsonrpc_routes(request_handler, "/a2a"))

app = Starlette(routes=routes, debug=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8003)
