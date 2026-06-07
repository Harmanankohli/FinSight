import asyncio
import atexit
import logging
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import click
import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from shared.a2a_store import SQLiteTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from shared.memory import SQLiteMemoryService, init_db
from shared.observability import init_langfuse, init_instrumentation, shutdown_langfuse

init_langfuse(service_name="orchestrator")
atexit.register(shutdown_langfuse)
init_instrumentation("orchestrator")

from shared.config import ADK_MODEL

from .agent import root_agent
from .agent_executor import FinSightAgentExecutor
from .agui_endpoint import make_agui_endpoint
from .agui_bridge import make_agui_bridge_endpoint
from .api_routes import get_api_routes

from shared.logging_config import setup_file_logging
setup_file_logging("orchestrator")
logger = logging.getLogger(__name__)

HOST = os.environ.get("HOST", "localhost")
PORT = int(os.environ.get("ORCHESTRATOR_PORT", "8001"))

if ADK_MODEL.startswith("gemini") and not os.getenv("GOOGLE_API_KEY"):
    logger.error("GOOGLE_API_KEY must be set for Gemini models")
    sys.exit(1)

agent_card = AgentCard(
    name="Investment Orchestrator",
    description="Coordinates specialized investment agents into a comprehensive Investment Brief",
    version="1.0.0",
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC",
            url=f"http://{HOST}:{PORT}/a2a",
        )
    ],
    default_input_modes=["text", "text/plain"],
    default_output_modes=["text", "application/json"],
    skills=[
        AgentSkill(
            id="investment_research",
            name="Investment Research",
            description="Answer investment queries with a complete research brief including RAG, quant, and market context analysis",
            tags=["investment", "stock research", "portfolio"],
            examples=[
                "Should I invest in NVDA given my current portfolio?",
                "Analyze AAPL for a long-term hold",
            ],
        )
    ],
)

task_store = SQLiteTaskStore()

# session_service: persists conversation turns; memory_service: enables semantic recall (load_memory tool)
session_service = DatabaseSessionService(
    db_url="sqlite+aiosqlite:///./db/adk_sessions.db"
)
memory_service = SQLiteMemoryService()

# ADK Runner: ties agent, session persistence, and memory service together for execution
runner = Runner(
    app_name=root_agent.name,
    agent=root_agent,
    session_service=session_service,
    memory_service=memory_service,
)

request_handler = DefaultRequestHandler(
    agent_executor=FinSightAgentExecutor(runner),
    task_store=task_store,
    agent_card=agent_card,
)

async def health(request):
    return JSONResponse({"status": "ok", "agent": "orchestrator"})


_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

# Three route groups: health check, AG-UI (RAGAS evaluation), and A2A protocol (sub-agent clients)
routes = [Route("/health", health)]
routes.append(Route("/agentic_chat", make_agui_endpoint(runner), methods=["POST"]))
routes.append(Route("/a2a-agui", make_agui_bridge_endpoint(runner), methods=["POST"]))
routes.extend(create_agent_card_routes(agent_card))
routes.extend(create_jsonrpc_routes(request_handler, "/a2a"))
routes.extend(get_api_routes())

# Serve generated reports as static files (PPTX/DOCX downloads)
from pathlib import Path as _Path
_reports_dir = _Path("db/reports")
_reports_dir.mkdir(parents=True, exist_ok=True)
routes.append(Mount("/reports", app=StaticFiles(directory=str(_reports_dir)), name="reports"))

app = Starlette(
    routes=routes,
    debug=True,
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=_ALLOWED_ORIGINS,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["X-FinSight-User-Id"],
        )
    ],
)


async def start_server(host: str, port: int) -> None:
    # Initialize SQLite tables before accepting connections
    from shared.memory.store import get_db, prune_old_records
    await get_db()
    logger.info("Memory layer initialized with persistent SQLite storage")

    # Best-effort pruning on startup — keeps DB from growing unbounded over months.
    try:
        deleted = await prune_old_records()
        if any(deleted.values()):
            logger.info("Pruned old memory records: %s", deleted)
    except Exception:
        logger.warning("Memory pruning failed (non-fatal)", exc_info=True)

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


@click.command()
@click.option("--host", default=HOST)
@click.option("--port", default=PORT)
def run(host: str, port: int) -> None:
    """Run the A2A investment orchestrator server."""
    asyncio.run(start_server(host, port))


if __name__ == "__main__":
    run()
