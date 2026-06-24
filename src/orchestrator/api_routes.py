"""REST API routes for the Next.js frontend.

All routes are read-only JSON endpoints. User identity is resolved from
the auth principal when auth is enabled, falling back to the
X-FinSight-User-Id request header in dev mode.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from shared.memory.store import get_db
from shared.rate_limiter import TokenBucket
from shared.settings import get_settings

logger = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"^[A-Z0-9.\^-]{1,10}$")
_api_rate_limiter = TokenBucket(rate=10.0, burst=20)
def _ERROR_ENVELOPE(code: str, msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": msg}}, status_code=status)


def _user_id(request: Request) -> str | None:
    """Resolve user ID from principal (auth mode) or X-FinSight-User-Id header (dev fallback)."""
    s = get_settings()
    if s.auth_enabled:
        from shared.auth.middleware import get_principal

        p = get_principal(request)
        if p and p.kind == "user":
            return p.subject
        return None
    return request.headers.get("X-FinSight-User-Id")


def _require_admin(request: Request) -> JSONResponse | None:
    """Return None if allowed, or 403 JSONResponse. No-op when auth is disabled."""
    s = get_settings()
    if not s.auth_enabled:
        return None
    from shared.auth.middleware import require
    from shared.auth.tokens import AuthError

    try:
        require(request, role="admin")
        return None
    except AuthError:
        return _ERROR_ENVELOPE("FORBIDDEN", "Admin access required", status=403)


def _parse_limit(request: Request, default: int = 10, max_val: int = 100) -> int | JSONResponse:
    raw = request.query_params.get("limit", str(default))
    try:
        val = int(raw)
    except (ValueError, TypeError):
        return _ERROR_ENVELOPE("VALIDATION_ERROR", f"'limit' must be an integer, got: {raw!r}")
    return max(1, min(val, max_val))


def _validate_ticker(symbol: str) -> str | JSONResponse:
    sym = symbol.upper()
    if not _TICKER_RE.match(sym):
        return _ERROR_ENVELOPE(
            "VALIDATION_ERROR",
            f"Invalid ticker '{symbol}'. Must match ^[A-Z0-9.^-]{{1,10}}$",
        )
    return sym


# ── /api/memory/ticker/{symbol} ───────────────────────────────────────────────


async def memory_ticker_history(request: Request) -> JSONResponse:
    sym = _validate_ticker(request.path_params["symbol"])
    if isinstance(sym, JSONResponse):
        return sym
    limit = _parse_limit(request)
    if isinstance(limit, JSONResponse):
        return limit
    user_id = _user_id(request)
    from shared.memory import TickerMemory

    tm = TickerMemory()
    history = await tm.get_history(sym, limit=limit, user_id=user_id)
    return JSONResponse(history)


async def memory_ticker_latest(request: Request) -> JSONResponse:
    sym = _validate_ticker(request.path_params["symbol"])
    if isinstance(sym, JSONResponse):
        return sym
    user_id = _user_id(request)
    from shared.memory import TickerMemory

    tm = TickerMemory()
    latest = await tm.get_latest(sym, user_id=user_id)
    if not latest:
        return _ERROR_ENVELOPE("NOT_FOUND", f"No briefs found for {sym}", status=404)
    return JSONResponse(latest)


async def memory_ticker_changed(request: Request) -> JSONResponse:
    sym = _validate_ticker(request.path_params["symbol"])
    if isinstance(sym, JSONResponse):
        return sym
    user_id = _user_id(request)
    from shared.memory import TickerMemory

    tm = TickerMemory()
    result = await tm.has_changed(sym, user_id=user_id)
    if result is None:
        return JSONResponse({"changed": False, "reason": "insufficient_history"})
    return JSONResponse(result)


# ── /api/sessions ─────────────────────────────────────────────────────────────


async def _check_rate_limit(request: Request) -> JSONResponse | None:
    if not await _api_rate_limiter.try_acquire():
        return _ERROR_ENVELOPE("RATE_LIMITED", "Too many requests", status=429)
    return None


async def sessions_list(request: Request) -> JSONResponse:
    """List sessions, optionally filtered by user_id."""
    rl = await _check_rate_limit(request)
    if rl:
        return rl
    limit = _parse_limit(request, default=50, max_val=200)
    if isinstance(limit, JSONResponse):
        return limit
    user_id = _user_id(request)
    if get_settings().auth_enabled:
        if user_id is None:
            return _ERROR_ENVELOPE("UNAUTHORIZED", "Authentication required", status=401)
    else:
        if not user_id:
            user_id = request.query_params.get("user_id")
    from shared.memory.session_repo import SessionRepo

    try:
        repo = SessionRepo()
        sessions = await repo.list_sessions(user_id=user_id, limit=limit)
        return JSONResponse(sessions)
    except Exception:
        logger.exception("Failed to list sessions")
        return _ERROR_ENVELOPE("INTERNAL", "Failed to list sessions", status=500)


async def session_events(request: Request) -> JSONResponse:
    """Return events for a specific session."""
    session_id = request.path_params["id"]
    user_id = _user_id(request)
    from shared.memory.session_repo import SessionRepo

    try:
        repo = SessionRepo()
        events = await repo.get_events(session_id, user_id=user_id)
        if events is None:
            return _ERROR_ENVELOPE("NOT_FOUND", "Session not found or no events", status=404)
        return JSONResponse({"session_id": session_id, "events": events})
    except Exception:
        logger.exception("Failed to load session events")
        return _ERROR_ENVELOPE("INTERNAL", "Failed to load session events", status=500)


# ── /api/reports ─────────────────────────────────────────────────────────────

_REPORT_CONTENT_TYPES = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "html": "text/html",
    "pdf": "application/pdf",
}

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


async def _build_report_response(
    brief_json_str: str | None,
    ticker: str,
    recommendation: str,
    confidence: float,
    analysis_date: str,
    fmt: str,
) -> Response:
    from shared.reports import generate_docx, generate_html, generate_pdf_async, generate_pptx_async

    brief_data: dict[str, Any] = {}
    if brief_json_str:
        try:
            brief_data = json.loads(brief_json_str)
        except json.JSONDecodeError:
            logger.debug("Could not parse brief_json for ticker report")

    safe_date = _SAFE_FILENAME_RE.sub("-", analysis_date or "report")
    filename = f"FinSight_{ticker}_{safe_date}.{fmt}"

    if fmt == "html":
        html_str = generate_html(
            brief_data, ticker, recommendation or "UNKNOWN", confidence or 0.0, analysis_date or ""
        )
        return Response(
            content=html_str,
            media_type="text/html",
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    if fmt == "pptx":
        buf = await generate_pptx_async(
            brief_data, ticker, recommendation or "UNKNOWN", confidence or 0.0, analysis_date or ""
        )
        return Response(
            content=buf.getvalue(),
            media_type=_REPORT_CONTENT_TYPES[fmt],
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    if fmt == "pdf":
        buf = await generate_pdf_async(
            brief_data, ticker, recommendation or "UNKNOWN", confidence or 0.0, analysis_date or ""
        )
        return Response(
            content=buf.getvalue(),
            media_type=_REPORT_CONTENT_TYPES[fmt],
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    buf = generate_docx(
        brief_data, ticker, recommendation or "UNKNOWN", confidence or 0.0, analysis_date or ""
    )

    return Response(
        content=buf.getvalue(),
        media_type=_REPORT_CONTENT_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def report_by_id(request: Request) -> Response:
    """Download PPTX, DOCX, or HTML for a specific brief by its database ID."""
    brief_id = request.path_params["brief_id"]
    fmt = request.path_params["format"].lower()
    if fmt not in _REPORT_CONTENT_TYPES:
        return _ERROR_ENVELOPE("VALIDATION_ERROR", "format must be pptx, docx, html, or pdf")

    user_id = _user_id(request)
    conn = await get_db()
    if get_settings().auth_enabled:
        if user_id is None:
            return _ERROR_ENVELOPE("UNAUTHORIZED", "Authentication required", status=401)
        cursor = await conn.execute(
            "SELECT ticker, recommendation, confidence, brief_json, analysis_date "
            "FROM ticker_briefs WHERE id = ? AND user_id = ?",
            (brief_id, user_id),
        )
    else:
        cursor = await conn.execute(
            "SELECT ticker, recommendation, confidence, brief_json, analysis_date "
            "FROM ticker_briefs WHERE id = ?",
            (brief_id,),
        )
    row = await cursor.fetchone()
    if not row:
        return _ERROR_ENVELOPE("NOT_FOUND", "Brief not found", status=404)

    ticker, rec, conf, brief_json_str, analysis_date = row
    return await _build_report_response(
        brief_json_str, ticker, rec, conf or 0.0, analysis_date or "", fmt
    )


async def report_latest(request: Request) -> Response:
    """Download PPTX, DOCX, or HTML for the most recent brief of a ticker."""
    sym = _validate_ticker(request.path_params["symbol"])
    if isinstance(sym, JSONResponse):
        return sym
    fmt = request.path_params["format"].lower()
    if fmt not in _REPORT_CONTENT_TYPES:
        return _ERROR_ENVELOPE("VALIDATION_ERROR", "format must be pptx, docx, html, or pdf")

    user_id = _user_id(request)
    from shared.memory import TickerMemory

    tm = TickerMemory()
    latest = await tm.get_latest(sym, user_id=user_id)
    if not latest:
        return _ERROR_ENVELOPE("NOT_FOUND", f"No briefs found for {sym}", status=404)

    return await _build_report_response(
        latest.get("brief_json"),
        latest["ticker"],
        latest.get("recommendation", "UNKNOWN"),
        latest.get("confidence") or 0.0,
        latest.get("analysis_date") or "",
        fmt,
    )


# ── /api/agents ───────────────────────────────────────────────────────────────


async def agents_list(request: Request) -> JSONResponse:
    """List discovered sub-agents with names, descriptions, and skills."""
    try:
        from orchestrator.agent import _client

        agents = _client.list_agents()
        return JSONResponse(agents)
    except Exception:
        logger.exception("agents_list failed")
        return _ERROR_ENVELOPE("INTERNAL", "Internal server error", status=500)


async def agent_health(request: Request) -> JSONResponse:
    """Fan-out health check to a named agent's /health endpoint."""
    denied = _require_admin(request)
    if denied:
        return denied
    name = request.path_params["name"]
    from orchestrator.agent import _client

    agents = {a["name"]: a for a in _client.list_agents()}
    if name not in agents:
        return _ERROR_ENVELOPE("NOT_FOUND", f"Unknown agent: {name}", status=404)

    base_url = _client.get_agent_base_url(name)
    if not base_url:
        return JSONResponse({"status": "unknown"})
    health_url = f"{base_url.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(health_url)
            return JSONResponse(
                {
                    "status": "ok" if resp.status_code == 200 else "degraded",
                    "detail": resp.json(),
                }
            )
    except Exception as exc:
        return JSONResponse({"status": "unreachable", "error": str(exc)})


# ── Route list ────────────────────────────────────────────────────────────────


def get_api_routes() -> list[Route]:
    return [
        Route("/api/memory/ticker/{symbol}", memory_ticker_history, methods=["GET"]),
        Route("/api/memory/ticker/{symbol}/latest", memory_ticker_latest, methods=["GET"]),
        Route("/api/memory/ticker/{symbol}/changed", memory_ticker_changed, methods=["GET"]),
        Route("/api/sessions", sessions_list, methods=["GET"]),
        Route("/api/sessions/{id}/events", session_events, methods=["GET"]),
        Route("/api/agents", agents_list, methods=["GET"]),
        Route("/api/agents/{name}/health", agent_health, methods=["GET"]),
        Route("/api/reports/ticker/{symbol}/latest/{format}", report_latest, methods=["GET"]),
        Route("/api/reports/{brief_id}/{format}", report_by_id, methods=["GET"]),
    ]
