"""REST API routes for the Next.js frontend.

All routes are read-only JSON endpoints. User identity is read from the
X-FinSight-User-Id request header.
"""

from __future__ import annotations

import json
import logging

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from shared.memory.store import DB_PATH, get_db, write_lock

logger = logging.getLogger(__name__)

_ADK_SESSION_DB = DB_PATH.parent / "adk_sessions.db"


def _user_id(request: Request) -> str | None:
    return request.headers.get("X-FinSight-User-Id")


# ── /api/memory/ticker/{symbol} ──────────────────────────────────────────────

async def memory_ticker_history(request: Request) -> JSONResponse:
    symbol = request.path_params["symbol"].upper()
    limit = int(request.query_params.get("limit", "10"))
    user_id = _user_id(request)
    from shared.memory import TickerMemory
    tm = TickerMemory()
    history = await tm.get_history(symbol, limit=limit, user_id=user_id)
    return JSONResponse(history)


async def memory_ticker_latest(request: Request) -> JSONResponse:
    symbol = request.path_params["symbol"].upper()
    from shared.memory import TickerMemory
    tm = TickerMemory()
    latest = await tm.get_latest(symbol)
    if not latest:
        return JSONResponse({"error": "No briefs found"}, status_code=404)
    return JSONResponse(latest)


async def memory_ticker_changed(request: Request) -> JSONResponse:
    symbol = request.path_params["symbol"].upper()
    from shared.memory import TickerMemory
    tm = TickerMemory()
    result = await tm.has_changed(symbol)
    if result is None:
        return JSONResponse({"changed": False, "reason": "insufficient_history"})
    return JSONResponse(result)


# ── /api/sessions ─────────────────────────────────────────────────────────────

async def sessions_list(request: Request) -> JSONResponse:
    """List all sessions (or filtered by user_id)."""
    import aiosqlite
    user_id = _user_id(request) or request.query_params.get("user_id")
    db = None
    try:
        db = await aiosqlite.connect(str(_ADK_SESSION_DB))
        if user_id:
            cursor = await db.execute(
                "SELECT id, user_id, app_name, update_time FROM sessions WHERE user_id = ? ORDER BY update_time DESC LIMIT 50",
                (user_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT id, user_id, app_name, update_time FROM sessions ORDER BY update_time DESC LIMIT 50"
            )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            sid = row[0]
            ev_cursor = await db.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = ?", (sid,)
            )
            ev_count = (await ev_cursor.fetchone())[0]
            result.append({
                "id": sid,
                "user_id": row[1],
                "app_name": row[2],
                "last_update_time": row[3],
                "event_count": ev_count,
            })
        return JSONResponse(result)
    except Exception as exc:
        logger.exception("Failed to list sessions")
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        if db is not None:
            await db.close()


async def session_events(request: Request) -> JSONResponse:
    """Return events for a specific session."""
    import json as _json
    import aiosqlite
    session_id = request.path_params["id"]
    db = None
    try:
        db = await aiosqlite.connect(str(_ADK_SESSION_DB))
        cursor = await db.execute(
            "SELECT id, user_id, timestamp, event_data FROM events WHERE session_id = ? ORDER BY timestamp",
            (session_id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return JSONResponse({"error": "Session not found or no events"}, status_code=404)
        events = []
        for row in rows:
            try:
                data = _json.loads(row[3]) if row[3] else {}
            except Exception:
                data = {}
            author = data.get("author") or data.get("role") or ""
            content_parts = []
            content = data.get("content", {})
            for part in (content.get("parts") or []):
                if isinstance(part, dict):
                    if part.get("text"):
                        content_parts.append({"type": "text", "text": part["text"]})
                    elif part.get("functionCall"):
                        fc = part["functionCall"]
                        content_parts.append({"type": "function_call", "name": fc.get("name", ""), "args": fc.get("args", {})})
                    elif part.get("functionResponse"):
                        fr = part["functionResponse"]
                        content_parts.append({"type": "function_response", "name": fr.get("name", ""), "response": str(fr.get("response", ""))[:500]})
            events.append({
                "id": row[0],
                "author": author,
                "timestamp": row[2],
                "content": content_parts,
            })
        return JSONResponse({"session_id": session_id, "events": events})
    except Exception as exc:
        logger.exception("Failed to load session events")
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        if db is not None:
            await db.close()


# ── /api/reports ─────────────────────────────────────────────────────────────

_REPORT_CONTENT_TYPES = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "html": "text/html",
}


async def _build_report_response(
    brief_json_str: str | None,
    ticker: str,
    recommendation: str,
    confidence: float,
    analysis_date: str,
    fmt: str,
) -> Response:
    from shared.report_generator import generate_pptx, generate_docx, generate_html

    brief_data: dict = {}
    if brief_json_str:
        try:
            brief_data = json.loads(brief_json_str)
        except json.JSONDecodeError:
            pass

    safe_date = (analysis_date or "report").replace(":", "-")
    filename = f"FinSight_{ticker}_{safe_date}.{fmt}"

    if fmt == "html":
        html_str = generate_html(brief_data, ticker, recommendation or "UNKNOWN",
                                 confidence or 0.0, analysis_date or "")
        return Response(
            content=html_str,
            media_type="text/html",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    generator = generate_pptx if fmt == "pptx" else generate_docx
    buf = generator(brief_data, ticker, recommendation or "UNKNOWN",
                    confidence or 0.0, analysis_date or "")

    return Response(
        content=buf.getvalue(),
        media_type=_REPORT_CONTENT_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def report_by_id(request: Request) -> Response:
    """Download PPTX or DOCX for a specific brief by its database ID."""
    brief_id = request.path_params["brief_id"]
    fmt = request.path_params["format"].lower()
    if fmt not in _REPORT_CONTENT_TYPES:
        return JSONResponse({"error": "format must be pptx, docx, or html"}, status_code=400)

    conn = await get_db()
    cursor = await conn.execute(
        "SELECT ticker, recommendation, confidence, brief_json, analysis_date "
        "FROM ticker_briefs WHERE id = ?",
        (brief_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return JSONResponse({"error": "Brief not found"}, status_code=404)

    ticker, rec, conf, brief_json_str, analysis_date = row
    return await _build_report_response(brief_json_str, ticker, rec, conf or 0.0, analysis_date or "", fmt)


async def report_latest(request: Request) -> Response:
    """Download PPTX or DOCX for the most recent brief of a ticker."""
    symbol = request.path_params["symbol"].upper()
    fmt = request.path_params["format"].lower()
    if fmt not in _REPORT_CONTENT_TYPES:
        return JSONResponse({"error": "format must be pptx, docx, or html"}, status_code=400)

    from shared.memory import TickerMemory
    tm = TickerMemory()
    latest = await tm.get_latest(symbol)
    if not latest:
        return JSONResponse({"error": f"No briefs found for {symbol}"}, status_code=404)

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
    from agent_1_adk.agent import _client
    agents = _client.list_agents()
    return JSONResponse(agents)


async def agent_health(request: Request) -> JSONResponse:
    """Fan-out health check to a named agent's /health endpoint."""
    name = request.path_params["name"]
    from agent_1_adk.agent import _client
    agents = {a["name"]: a for a in _client.list_agents()}
    if name not in agents:
        return JSONResponse({"error": f"Unknown agent: {name}"}, status_code=404)

    # The agent card URL ends with /.well-known/agent-card — strip to get base
    entry = _client._agents.get(name)
    if not entry:
        return JSONResponse({"status": "unknown"})
    base_url = entry["url"].rstrip("/")
    health_url = f"{base_url}/health"
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(health_url)
            return JSONResponse({"status": "ok" if resp.status_code == 200 else "degraded", "detail": resp.json()})
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
        Route("/api/reports/{brief_id}/{format}", report_by_id, methods=["GET"]),
        Route("/api/reports/ticker/{symbol}/latest/{format}", report_latest, methods=["GET"]),
    ]
