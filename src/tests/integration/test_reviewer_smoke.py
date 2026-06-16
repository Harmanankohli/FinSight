"""Smoke test for the Reviewer Agent (OpenAI Agents SDK).

Marked @pytest.mark.integration — skipped by default, run with:
    pytest -m integration

Requires all services to be running (run_adk_web.bat).
"""

import httpx
import pytest


@pytest.mark.integration
@pytest.mark.external
async def test_reviewer_server_is_reachable():
    """Reviewer agent (port 8006) responds to HTTP requests."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("http://localhost:8006/")
    assert resp.status_code in (200, 404, 405), (
        f"Server unreachable or unexpected status: {resp.status_code}"
    )


@pytest.mark.integration
@pytest.mark.external
async def test_reviewer_agent_card_reachable():
    """Reviewer agent (port 8006) exposes its A2A agent card."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("http://localhost:8006/.well-known/agent-card")
    assert resp.status_code == 200
    card = resp.json()
    assert "name" in card
    assert card["name"] == "Reviewer Agent"


@pytest.mark.integration
@pytest.mark.external
async def test_reviewer_health_endpoint():
    """Reviewer agent (port 8006) exposes /health endpoint."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("http://localhost:8006/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"


@pytest.mark.integration
@pytest.mark.external
async def test_reviewer_agent_card_skills():
    """Reviewer agent card lists expected skills."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("http://localhost:8006/.well-known/agent-card")
    card = resp.json()
    skills = [s["id"] for s in card.get("skills", [])]
    expected = {
        "contradiction_check",
        "source_verification",
        "confidence_scoring",
        "recommendation_validation",
        "structured_review",
    }
    assert expected.issubset(set(skills)), f"Missing skills: {expected - set(skills)}"
