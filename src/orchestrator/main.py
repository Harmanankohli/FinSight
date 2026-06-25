"""Entry point for the A2A investment orchestrator server: Starlette app, routes, and lifecycle.

Builds and runs the orchestrator Starlette application with A2A protocol routes,
REST API routes, authentication middleware, and background sub-agent discovery.
"""

# ruff: noqa: E402
import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from shared.bootstrap import bootstrap

_settings = bootstrap("orchestrator")

import click
import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from shared.a2a_store import SQLiteTaskStore
from shared.memory.memory_service import SQLiteMemoryService
from shared.observability import init_instrumentation

init_instrumentation("orchestrator")

from shared.settings import ADK_MODEL

from .agent import root_agent, start_background_discovery
from .agent_executor import FinSightAgentExecutor
from .agui_bridge import make_agui_bridge_endpoint
from .agui_endpoint import make_agui_endpoint
from .api_routes import get_api_routes
from .auth_routes import get_auth_routes

logger = logging.getLogger(__name__)

HOST = _settings.host
PORT = _settings.orchestrator_port

if ADK_MODEL.startswith("gemini") and not _settings.langfuse_public_key:
    pass  # GOOGLE_API_KEY checked at runtime if gemini models are used

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
            description="Answer investment queries with a complete research brief including RAG, quant, and market context analysis",  # noqa: E501
            tags=["investment", "stock research", "portfolio"],
            examples=[
                "Should I invest in NVDA given my current portfolio?",
                "Analyze AAPL for a long-term hold",
            ],
        )
    ],
)

# Add bearer auth security scheme to the agent card
_scheme = agent_card.security_schemes.get_or_create("bearerAuth")
_scheme.http_auth_security_scheme.scheme = "bearer"
_scheme.http_auth_security_scheme.bearer_format = "JWT"
_req = agent_card.security_requirements.add()
_req.schemes.get_or_create("bearerAuth")

task_store = SQLiteTaskStore()

# session_service: persists conversation turns; memory_service: enables semantic recall (load_memory tool)  # noqa: E501
session_service = DatabaseSessionService(db_url="sqlite+aiosqlite:///./db/adk_sessions.db")
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


async def health(request: Request) -> JSONResponse:
    """Return a simple health-check JSON response."""

    return JSONResponse({"status": "ok", "agent": "orchestrator"})


_ALLOWED_ORIGINS = [o.strip() for o in _settings.cors_origins.split(",") if o.strip()]

from shared.auth.middleware import build_auth_middleware

# Three route groups: health check, AG-UI (RAGAS evaluation), A2A protocol, auth, and REST API
routes = [Route("/health", health)]
routes.append(Route("/agentic_chat", make_agui_endpoint(runner), methods=["POST"]))
routes.append(Route("/a2a-agui", make_agui_bridge_endpoint(runner), methods=["POST"]))
routes.extend(create_agent_card_routes(agent_card))
routes.extend(create_jsonrpc_routes(request_handler, "/a2a"))
routes.extend(get_api_routes())
routes.extend(get_auth_routes())

# Serve generated reports as static files (PPTX/DOCX downloads) — authenticated
from pathlib import Path as _Path

_reports_dir = _Path("db/reports")
_reports_dir.mkdir(parents=True, exist_ok=True)
from starlette.responses import FileResponse

from shared.auth.middleware import require as _require_auth

_SAFE_FILENAME_RE = __import__("re", fromlist=["compile"]).compile(r"[^A-Za-z0-9._-]")


async def _authenticated_report(request: Request) -> JSONResponse | FileResponse:
    """GET /reports/{filename} — authenticated report download."""
    try:
        _require_auth(request, kinds=("user", "service"))
    except Exception:
        return JSONResponse(
            {"error": {"code": "UNAUTHENTICATED", "message": "Authentication required"}},
            status_code=401,
        )
    filename = request.path_params.get("filename", "")
    if not _SAFE_FILENAME_RE.sub("", filename) == filename:
        return JSONResponse(
            {"error": {"code": "VALIDATION_ERROR", "message": "Invalid filename"}}, status_code=400
        )
    file_path = (_reports_dir / filename).resolve()
    try:
        file_path.relative_to(_reports_dir.resolve())
    except ValueError:
        return JSONResponse(
            {"error": {"code": "FORBIDDEN", "message": "Path traversal blocked"}}, status_code=403
        )
    if not file_path.is_file():
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "File not found"}}, status_code=404
        )
    return FileResponse(str(file_path))


routes.append(Route("/reports/{filename}", _authenticated_report, methods=["GET"]))


@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
    """Start background sub-agent discovery on startup and cleanly
    disconnect MCP clients on shutdown."""

    await start_background_discovery()
    yield
    from shared.mcp_client import _global_client

    if _global_client and _global_client._connected:
        try:
            await _global_client.disconnect_all()
        except Exception:
            logger.warning("MCP disconnect during shutdown", exc_info=True)


app = Starlette(
    routes=routes,
    debug=_settings.env != "production",
    lifespan=lifespan,
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=_ALLOWED_ORIGINS,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["X-FinSight-User-Id"],
        ),
    ]
    + build_auth_middleware(_settings),
)


async def start_server(host: str, port: int) -> None:
    """Initialise storage, prune stale records, then start the uvicorn HTTP server."""
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

    # Prune stale agent outputs from the shared store (TTL-based cleanup)
    try:
        from shared.memory.agent_output_store import prune_stale_outputs

        stale_count = await prune_stale_outputs(max_age_seconds=600)
        if stale_count:
            logger.info("Pruned %d stale agent outputs", stale_count)
    except Exception:
        logger.warning("Agent output pruning failed (non-fatal)", exc_info=True)

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
