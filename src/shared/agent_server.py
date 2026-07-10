"""Sub-agent server factory — builds a Starlette ASGI app for A2A sub-agents.

Usage:
    from shared.agent_server import build_agent_app
    app = build_agent_app(agent_card=card, agent=MyAgent(), service_name="my_agent")
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

# A2A (Agent-to-Agent) protocol primitives — request handling, route generation, and card types.
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.types import AgentCard
# Starlette ASGI framework — lightweight app shell, JSON responses, and URL routing.
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

# Shared project constructs — persistent task store, agent base class, and sync executor wrapper.
from shared.a2a_store import SQLiteTaskStore
from shared.base_agent import BaseAgent
from shared.generic_executor import GenericAgentExecutor

logger = logging.getLogger(__name__)


def build_auth_middleware(settings: Any, accept: frozenset[str] | None = None) -> list[Any]:
    """Build auth middleware list from the shared auth toolkit.

    Re-exported for backward compatibility with WP 1.4 callers.
    Delegates to shared.auth.middleware.build_auth_middleware.
    """
    # Late import to avoid circular dependency at module load time.
    from shared.auth.middleware import build_auth_middleware as _build

    return _build(settings, accept=accept)


def _health(service_name: str) -> Callable[..., Any]:
    """Factory: return a handler that responds with agent identity and liveness status."""
    async def handler(request: Any) -> JSONResponse:
        return JSONResponse({"status": "ok", "agent": service_name})

    return handler


async def _release_evals(request: Any) -> JSONResponse:
    """POST /release-evals — flush any queued evaluation tasks and return the count released."""
    from shared.eval_gate import release_evals as _release

    n = await _release()
    return JSONResponse({"released": n})


def _add_security_to_card(card: AgentCard, *, accept: frozenset[str] | None = None) -> None:
    """Add bearer auth security schemes and requirements to an AgentCard.

    Ensures the AgentCard advertises JWT bearer auth. When *accept* restricts
    to service-only principals, the security requirement list is narrowed accordingly
    so that the card schema reflects which credential types are allowed.
    """
    # Configure the bearer scheme to require JWT-format tokens.
    card.security_schemes["bearerAuth"].http_auth_security_scheme.scheme = "bearer"
    card.security_schemes["bearerAuth"].http_auth_security_scheme.bearer_format = "JWT"

    # Populate security requirements so consumers know what auth scopes to send.
    req = card.security_requirements.add()
    sl = req.schemes["bearerAuth"]
    # When accept is explicitly service-only (no user tokens), restrict the requirement list.
    if accept is not None and "service" in accept and "user" not in accept:
        sl.list.append("service")


def build_agent_app(
    *,
    agent_card: AgentCard,
    agent: BaseAgent,
    service_name: str,
    on_startup: Sequence[Callable[..., Any]] = (),
    extra_routes: Sequence[Route] = (),
    accept: frozenset[str] | None = None,
) -> Starlette:
    """Build a Starlette ASGI app for an A2A sub-agent.

    Includes /health, /release-evals, A2A card routes, and JSON-RPC routes.
    bootstrap() is called here if not already called by the entrypoint.

    *accept* controls which principal kinds pass the auth middleware:
    - ``None`` (default): both ``user`` and ``service``
    - ``frozenset({"service"})``: only service tokens (sub-agents)
    """
    # Fetch settings once so all downstream consumers share the same state.
    from shared.settings import get_settings

    settings = get_settings()

    # Stamp the agent card with auth scheme metadata before exposing it via A2A card routes.
    _add_security_to_card(agent_card, accept=accept)

    # Wire the A2A request handler: the GenericAgentExecutor adapts the agent's
    # sync interface to the async executor protocol expected by the A2A server.
    handler = DefaultRequestHandler(
        agent_executor=GenericAgentExecutor(agent),
        task_store=SQLiteTaskStore(),
        agent_card=agent_card,
    )

    # Assemble the route table — health probes, eval gate, optional extra routes,
    # agent card discovery, and the A2A JSON-RPC endpoint.
    routes: list[Route] = [
        Route("/health", _health(service_name)),
        Route("/release-evals", _release_evals, methods=["POST"]),
        *extra_routes,
    ]
    # A2A card routes expose the agent's capabilities and security requirements.
    routes.extend(create_agent_card_routes(agent_card))
    # JSON-RPC routes handle the core A2A task lifecycle (send, get, cancel, etc.)
    # under the /a2a path prefix.
    routes.extend(create_jsonrpc_routes(handler, "/a2a"))

    logger.info(
        "Building agent app for %s (auth=%s, routes=%d)",
        service_name,
        accept if accept else "user+service",
        len(routes),
    )
    return Starlette(
        routes=routes,
        on_startup=list(on_startup),
        # Disable debug mode in production to avoid leaking stack traces.
        debug=settings.env != "production",
        middleware=build_auth_middleware(settings, accept=accept),
    )
