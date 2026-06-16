"""Smoke test for the Analytics Agent (PydanticAI + pydantic-graph).

Marked @pytest.mark.integration — skipped by default, run with:
    pytest -m integration

Requires all services to be running (run_adk_web.bat).
"""

import httpx
import pytest


@pytest.mark.integration
@pytest.mark.external
async def test_analytics_server_is_reachable():
    """Analytics agent (port 8005) responds to HTTP requests."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("http://localhost:8005/")
    assert resp.status_code in (200, 404, 405), (
        f"Server unreachable or unexpected status: {resp.status_code}"
    )


@pytest.mark.integration
@pytest.mark.external
async def test_analytics_agent_card_reachable():
    """Analytics agent (port 8005) exposes its A2A agent card."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("http://localhost:8005/.well-known/agent-card")
    assert resp.status_code == 200
    card = resp.json()
    assert "name" in card
    assert card["name"] == "Analytics Agent"


@pytest.mark.integration
@pytest.mark.external
async def test_analytics_health_endpoint():
    """Analytics agent (port 8005) exposes /health endpoint."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("http://localhost:8005/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"


@pytest.mark.integration
@pytest.mark.external
async def test_analytics_agent_card_skills():
    """Analytics agent card lists expected skills."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("http://localhost:8005/.well-known/agent-card")
    card = resp.json()
    skills = [s["id"] for s in card.get("skills", [])]
    expected = {
        "trend_detection",
        "forecasting",
        "chart_generation",
        "statistical_analysis",
        "anomaly_detection",
    }
    assert expected.issubset(set(skills)), f"Missing skills: {expected - set(skills)}"
