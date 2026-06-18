# ruff: noqa: E501
"""FastAPI app for REST API documentation and OpenAPI spec generation.

WP 3.2: this FastAPI app shadows the Starlette REST routes for OpenAPI spec
generation. It is NOT used for request handling at runtime — the Starlette
routes in main.py handle all requests. This app exists solely to produce
the docs/openapi.json specification.

Usage:
    python -c "from orchestrator.api_fastapi import spec; import json; print(json.dumps(spec, indent=2))"  # noqa: E501
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException

logger = logging.getLogger(__name__)

from shared.models import (
    AgentHealthResponse,
    AgentListItem,
    ErrorResponse,
    HealthResponse,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    MemoryTickerChangedResponse,
    MemoryTickerItem,
    MeResponse,
    SessionEventsResponse,
    SessionListItem,
    TokenResponse,
)

fastapi_app = FastAPI(
    title="FinSight API",
    description="REST API for the FinSight multi-agent investment system",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


@fastapi_app.get("/health", response_model=HealthResponse, tags=["System"])
async def health():
    return {"status": "ok", "agent": "orchestrator"}


@fastapi_app.get(
    "/api/memory/ticker/{symbol}",
    response_model=list[MemoryTickerItem],
    responses={400: {"model": ErrorResponse}},
    tags=["Memory"],
    summary="List ticker memory history",
)
async def memory_ticker_history(symbol: str, limit: int = 10):
    logger.warning("Unimplemented endpoint called: GET /api/memory/ticker/%s", symbol)
    raise HTTPException(501)


@fastapi_app.get(
    "/api/memory/ticker/{symbol}/latest",
    response_model=MemoryTickerItem,
    responses={404: {"model": ErrorResponse}},
    tags=["Memory"],
    summary="Get latest ticker brief",
)
async def memory_ticker_latest(symbol: str):
    logger.warning("Unimplemented endpoint called: GET /api/memory/ticker/%s/latest", symbol)
    raise HTTPException(501)


@fastapi_app.get(
    "/api/memory/ticker/{symbol}/changed",
    response_model=MemoryTickerChangedResponse,
    tags=["Memory"],
    summary="Check if ticker recommendation changed",
)
async def memory_ticker_changed(symbol: str):
    logger.warning("Unimplemented endpoint called: GET /api/memory/ticker/%s/changed", symbol)
    raise HTTPException(501)


@fastapi_app.get(
    "/api/sessions",
    response_model=list[SessionListItem],
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    tags=["Sessions"],
    summary="List conversation sessions",
)
async def sessions_list(user_id: str | None = None, limit: int = 50):
    logger.warning("Unimplemented endpoint called: GET /api/sessions")
    raise HTTPException(501)


@fastapi_app.get(
    "/api/sessions/{id}/events",
    response_model=SessionEventsResponse,
    responses={404: {"model": ErrorResponse}},
    tags=["Sessions"],
    summary="Get session events",
)
async def session_events(id: str):
    logger.warning("Unimplemented endpoint called: GET /api/sessions/%s/events", id)
    raise HTTPException(501)


@fastapi_app.get(
    "/api/agents",
    response_model=list[AgentListItem],
    tags=["Agents"],
    summary="List discovered sub-agents",
)
async def agents_list():
    logger.warning("Unimplemented endpoint called: GET /api/agents")
    raise HTTPException(501)


@fastapi_app.get(
    "/api/agents/{name}/health",
    response_model=AgentHealthResponse,
    responses={404: {"model": ErrorResponse}},
    tags=["Agents"],
    summary="Check sub-agent health",
)
async def agent_health(name: str):
    logger.warning("Unimplemented endpoint called: GET /api/agents/%s/health", name)
    raise HTTPException(501)


@fastapi_app.get(
    "/api/reports/ticker/{symbol}/latest/{format}",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    tags=["Reports"],
    summary="Download latest ticker report",
)
async def report_latest(symbol: str, format: str):
    logger.warning("Unimplemented endpoint called: GET /api/reports/ticker/%s/latest/%s", symbol, format)
    raise HTTPException(501)


@fastapi_app.get(
    "/api/reports/{brief_id}/{format}",
    responses={404: {"model": ErrorResponse}},
    tags=["Reports"],
    summary="Download report by brief ID",
)
async def report_by_id(brief_id: str, format: str):
    logger.warning("Unimplemented endpoint called: GET /api/reports/%s/%s", brief_id, format)
    raise HTTPException(501)


@fastapi_app.post(
    "/auth/login",
    response_model=LoginResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
    tags=["Auth"],
    summary="Authenticate with username+password",
)
async def login(body: LoginRequest):
    logger.warning("Unimplemented endpoint called: POST /auth/login")
    raise HTTPException(501)


@fastapi_app.post(
    "/auth/refresh",
    response_model=TokenResponse,
    responses={401: {"model": ErrorResponse}},
    tags=["Auth"],
    summary="Refresh access token",
)
async def refresh():
    logger.warning("Unimplemented endpoint called: POST /auth/refresh")
    raise HTTPException(501)


@fastapi_app.post(
    "/auth/logout",
    response_model=LogoutResponse,
    tags=["Auth"],
    summary="Revoke refresh token and clear session",
)
async def logout():
    logger.warning("Unimplemented endpoint called: POST /auth/logout")
    raise HTTPException(501)


@fastapi_app.get(
    "/auth/me",
    response_model=MeResponse,
    responses={401: {"model": ErrorResponse}},
    tags=["Auth"],
    summary="Get current authenticated user",
)
async def me():
    logger.warning("Unimplemented endpoint called: GET /auth/me")
    raise HTTPException(501)


spec = fastapi_app.openapi()
