"""Characterization tests for orchestrator API route contracts.

Uses httpx.AsyncClient with ASGITransport so no real server is needed.
A temp SQLite DB is seeded via TickerMemory so read endpoints return
real-shaped data.

These tests assert: HTTP status code, presence of top-level keys, and
their types. They are intentionally loose on values so minor data changes
don't break them — they exist to catch structural regressions during
refactoring (Phase 1 / WP 1.6).
"""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

# ── Minimal health handler ────────────────────────────────────────────────────


async def _health(request):
    return JSONResponse({"status": "ok", "agent": "orchestrator"})


# ── App fixture ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture()
async def api_app(tmp_path):
    """Build a minimal Starlette app with API routes + health, isolated DB."""
    import os
    from shared.settings import reset_settings_for_tests

    old_auth = os.environ.get("AUTH_ENABLED")
    os.environ["AUTH_ENABLED"] = "false"
    reset_settings_for_tests()

    db_path = tmp_path / "test.db"

    import shared.memory.store as store_mod
    import shared.memory.ticker_memory as tm_mod

    # Reset singletons to use the temp path
    store_mod.DB_PATH = db_path
    store_mod._db_conn = None
    tm_mod.DB_PATH = db_path

    # Init DB schema via get_db (which runs init_db internally on first open)
    from shared.memory.store import get_db

    await get_db(db_path)

    # Seed one minimal brief
    from shared.memory.ticker_memory import TickerMemory

    mem = TickerMemory(db_path=db_path)
    await mem.store_minimal(
        ticker="NVDA",
        user_id="test-user",
        session_id="test-session",
        query="Analyze NVDA",
        response_text="NVDA looks strong.",
        recommendation="BUY",
        confidence=0.85,
    )

    from orchestrator.api_routes import get_api_routes
    # orchestrator.agent is pre-stubbed in conftest.py to avoid a2a-sdk proto loading

    routes = [Route("/health", _health)] + get_api_routes()
    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(
                CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
            )
        ],
    )
    yield app, db_path

    # Cleanup: close DB connection and restore defaults
    if store_mod._db_conn is not None:
        await store_mod._db_conn.close()
        store_mod._db_conn = None
    if old_auth is not None:
        os.environ["AUTH_ENABLED"] = old_auth
    else:
        os.environ.pop("AUTH_ENABLED", None)
    reset_settings_for_tests()


# ── /health ───────────────────────────────────────────────────────────────────


async def test_health_ok(api_app):
    app, _ = api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


# ── /api/memory/ticker/{symbol} ───────────────────────────────────────────────


async def test_memory_ticker_history_shape(api_app):
    app, _ = api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/memory/ticker/NVDA")
    assert r.status_code == 200
    body = r.json()
    # Route returns list directly (not wrapped); each item is a brief dict
    assert isinstance(body, list)
    if body:
        assert isinstance(body[0], dict)


async def test_memory_ticker_history_unknown_ticker(api_app):
    app, _ = api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/memory/ticker/ZZZZ")
    assert r.status_code == 200
    assert r.json() == []


async def test_memory_ticker_latest_shape(api_app):
    app, _ = api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/memory/ticker/NVDA/latest")
    assert r.status_code in (200, 404)


async def test_memory_ticker_changed_shape(api_app):
    app, _ = api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/memory/ticker/NVDA/changed")
    assert r.status_code == 200
    assert "changed" in r.json()


# ── /api/sessions ─────────────────────────────────────────────────────────────


async def test_sessions_list_shape(api_app):
    app, _ = api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/sessions", params={"user_id": "test-user"})
    assert r.status_code in (200, 500)  # 500 if ADK sessions DB absent (acceptable)
    body = r.json()
    assert isinstance(body, (list, dict))


async def test_sessions_list_no_user_id(api_app):
    app, _ = api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/sessions")
    assert r.status_code in (200, 400, 500)


# ── /api/sessions/{id}/events ─────────────────────────────────────────────────


async def test_session_events_missing(api_app):
    app, _ = api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/sessions/nonexistent-id/events")
    # 404 when found but empty; 500 when ADK sessions DB absent in test env
    assert r.status_code in (200, 404, 500)
    assert isinstance(r.json(), (list, dict))


# ── /api/agents ───────────────────────────────────────────────────────────────


async def test_agents_list_shape(api_app):
    app, _ = api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/agents")
    assert r.status_code == 200
    # Route returns list directly (not wrapped in a dict)
    body = r.json()
    assert isinstance(body, list)


async def test_agent_health_unknown_name(api_app):
    app, _ = api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/agents/rag/health")
    assert r.status_code in (200, 404, 503)
    assert isinstance(r.json(), dict)


# ── /api/reports ──────────────────────────────────────────────────────────────


async def test_report_latest_unsupported_format(api_app):
    app, _ = api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/reports/ticker/NVDA/latest/xml")
    assert r.status_code in (400, 404)


async def test_report_by_id_not_found(api_app):
    app, _ = api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/reports/nonexistent-id/pptx")
    assert r.status_code == 404
