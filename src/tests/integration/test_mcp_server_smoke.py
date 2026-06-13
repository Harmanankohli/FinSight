"""Smoke test for the MCP / Starlette server.

Marked @pytest.mark.integration — skipped by default, run with:
    pytest -m integration

Requires all services to be running (run_adk_web.bat).
"""

import httpx
import pytest


@pytest.mark.integration
@pytest.mark.external
async def test_mcp_server_is_reachable():
    """MCP server (port 8010) responds to HTTP requests."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("http://localhost:8010/")
    assert resp.status_code in (200, 404, 405), (
        f"Server unreachable or unexpected status: {resp.status_code}"
    )


@pytest.mark.integration
@pytest.mark.external
async def test_rag_agent_card_reachable():
    """RAG agent (port 8002) exposes its A2A agent card."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("http://localhost:8002/.well-known/agent-card")
    assert resp.status_code == 200
    card = resp.json()
    assert "name" in card


@pytest.mark.integration
@pytest.mark.external
async def test_quant_agent_card_reachable():
    """Quant agent (port 8003) exposes its A2A agent card."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("http://localhost:8003/.well-known/agent-card")
    assert resp.status_code == 200
    card = resp.json()
    assert "name" in card


@pytest.mark.integration
@pytest.mark.external
async def test_market_context_agent_card_reachable():
    """Market Context agent (port 8004) exposes its A2A agent card."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("http://localhost:8004/.well-known/agent-card")
    assert resp.status_code == 200
    card = resp.json()
    assert "name" in card
    assert card["name"] == "Market Context Agent"
