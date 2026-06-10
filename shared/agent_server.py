"""Sub-agent server factory — builds a Starlette ASGI app for A2A sub-agents.

Usage:
    from shared.agent_server import build_agent_app
    app = build_agent_app(agent_card=card, agent=MyAgent(), service_name="my_agent")
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.types import AgentCard
from shared.a2a_store import SQLiteTaskStore
from shared.base_agent import BaseAgent
from shared.bootstrap import bootstrap
from shared.generic_executor import GenericAgentExecutor
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route


def build_auth_middleware(settings) -> list:  # type: ignore[type-arg]
    """Phase-1 stub: returns empty list (auth off).

    WP 2.1 replaces this with real AuthMiddleware and re-exports from here.
    """
    return []


def _health(service_name: str):
    async def handler(request):
        return JSONResponse({"status": "ok", "agent": service_name})
    return handler


async def _release_evals(request):
    from shared.eval_gate import release_evals as _release
    n = await _release()
    return JSONResponse({"released": n})


def build_agent_app(
    *,
    agent_card: AgentCard,
    agent: BaseAgent,
    service_name: str,
    on_startup: Sequence[Callable] = (),
    extra_routes: Sequence[Route] = (),
) -> Starlette:
    """Build a Starlette ASGI app for an A2A sub-agent.

    Includes /health, /release-evals, A2A card routes, and JSON-RPC routes.
    bootstrap() is called here if not already called by the entrypoint.
    """
    from shared.settings import get_settings
    settings = get_settings()

    handler = DefaultRequestHandler(
        agent_executor=GenericAgentExecutor(agent),
        task_store=SQLiteTaskStore(),
        agent_card=agent_card,
    )

    routes: list[Route] = [
        Route("/health", _health(service_name)),
        Route("/release-evals", _release_evals, methods=["POST"]),
        *extra_routes,
    ]
    routes.extend(create_agent_card_routes(agent_card))
    routes.extend(create_jsonrpc_routes(handler, "/a2a"))

    return Starlette(
        routes=routes,
        on_startup=list(on_startup),
        debug=settings.env != "production",
        middleware=build_auth_middleware(settings),
    )
