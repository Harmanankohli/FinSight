"""Unified MCP data server providing financial data tools — composition root.

Exposes stock prices, financials, SEC EDGAR filings, news sentiment, ticker
resolution, and a hardened Python sandbox — all over the Model Context Protocol.

Agents consume these tools through the FastMCP SSE endpoint; no REST layer needed.

Architecture
------------
- ``mcp_servers._app``         : FastMCP singleton (``app``)
- ``mcp_servers.infra``        : rate limiters, caches, news helpers, embed loader
- ``mcp_servers.tools``        : all @app.tool() registrations (imported for side-effects)
"""

from __future__ import annotations

import logging
import threading

from shared.bootstrap import bootstrap

_settings = bootstrap("mcp")

# ── server globals ───────────────────────────────────────────────────────────
HOST = _settings.mcp_host
PORT = _settings.mcp_port

# ── import app singleton (creates FastMCP instance) ──────────────────────────
from mcp_servers._app import app  # noqa: E402  (must follow bootstrap)

# ── trigger all @app.tool() registrations ────────────────────────────────────
# tools/__init__ imports agent_registry which reads EMBED_MODEL_NAME lazily;
# patch the default before any tool is first called (not at import time).
import mcp_servers.tools  # noqa: F401  (side-effect: registers all tools)
import mcp_servers.tools.agent_registry as _ar_mod
_ar_mod.EMBED_MODEL_NAME = _settings.embed_model

logger = logging.getLogger(__name__)

# ── thread-safe ASGI app singleton ───────────────────────────────────────────

_starlette_app = None
_app_lock = threading.Lock()


def get_app():
    """Thread-safe lazy singleton for the ASGI server app.

    The Starlette app wraps the FastMCP SSE endpoint alongside a /health route.
    Lazily constructing it means uvicorn can import this module without
    eagerly loading starlette into memory until the server actually starts.
    Double-checked locking ensures only one caller creates the app wrapper.
    """
    global _starlette_app
    if _starlette_app is None:
        with _app_lock:
            if _starlette_app is None:
                from starlette.applications import Starlette
                from starlette.routing import Route, Mount
                from starlette.responses import JSONResponse

                async def health(request):
                    return JSONResponse({"status": "ok", "agent": "mcp"})

                mcp_asgi = app.sse_app()
                _starlette_app = Starlette(routes=[
                    Route("/health", health),
                    Mount("/", app=mcp_asgi),
                ])
    return _starlette_app


# Entry point for `python finsight_server.py`. Uvicorn is imported lazily here
# so that importing the module (e.g. for FastMCP SSE app refs) doesn't eagerly
# pull in the ASGI runtime — keeps import chains clean for agent tests.
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(get_app(), host=HOST, port=PORT)
