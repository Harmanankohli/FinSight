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

setup_file_logging("sentiment")
init_langfuse(service_name="sentiment_agent")
atexit.register(shutdown_langfuse)
from openinference.instrumentation.crewai import CrewAIInstrumentor

CrewAIInstrumentor().instrument(skip_dep_check=True)
from opentelemetry.instrumentation.starlette import StarletteInstrumentor

StarletteInstrumentor().instrument()

from starlette.responses import JSONResponse
from starlette.routing import Route

from shared.generic_executor import GenericAgentExecutor
from .executor import SentimentAgent

logger = logging.getLogger(__name__)


async def health(request):
    # Health check for orchestrator-level monitoring and container orchestration probes
    return JSONResponse({"status": "ok", "agent": "sentiment"})

host = os.environ.get("HOST", "localhost")

agent_card = AgentCard(
    name="Sentiment Intelligence Agent",
    description="Synthesizes financial news sentiment, SEC insider trading signals, and investment narratives using CrewAI",
    version="1.0.0",
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
            id="sentiment_analysis",
            name="Sentiment & Narrative Intelligence",
            description="Analyze financial news sentiment, SEC filings for insider context, produce investment narrative with key risks and catalysts",
            tags=["sentiment", "news", "insider trading", "narrative", "market sentiment"],
            examples=[
                "What is the market sentiment for NVDA?",
                "Analyze insider trading activity for AAPL",
            ],
        )
    ],
)

request_handler = DefaultRequestHandler(
    agent_executor=GenericAgentExecutor(SentimentAgent()),
    task_store=SQLiteTaskStore(),
    agent_card=agent_card,
)

routes = [Route("/health", health)]  # Liveness check
routes.extend(create_agent_card_routes(agent_card))  # A2A agent card discovery
routes.extend(create_jsonrpc_routes(request_handler, "/a2a"))  # JSON-RPC task endpoints

app = Starlette(routes=routes, debug=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8004)
