"""Verify the OpenAPI spec is in sync with the FastAPI documentation app.

WP 3.2: regenerate with `python scripts/generate_openapi.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.openapi

OPENAPI_PATH = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "openapi.json"


def test_openapi_spec_exists():
    assert OPENAPI_PATH.exists(), f"OpenAPI spec not found at {OPENAPI_PATH}"


def test_openapi_spec_is_valid_json():
    spec = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert "openapi" in spec
    assert "info" in spec
    assert "paths" in spec


def test_openapi_spec_has_required_paths():
    spec = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    paths = spec.get("paths", {})
    required = [
        "/api/memory/ticker/{symbol}",
        "/api/memory/ticker/{symbol}/latest",
        "/api/memory/ticker/{symbol}/changed",
        "/api/sessions",
        "/api/sessions/{id}/events",
        "/api/agents",
        "/api/agents/{name}/health",
        "/api/reports/ticker/{symbol}/latest/{format}",
        "/api/reports/{brief_id}/{format}",
        "/auth/login",
        "/auth/refresh",
        "/auth/logout",
        "/auth/me",
    ]
    for path in required:
        assert path in paths, f"Missing required path: {path}"


def test_openapi_spec_has_required_schemas():
    spec = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    schemas = spec.get("components", {}).get("schemas", {})
    required = [
        "HealthResponse",
        "LoginRequest",
        "LoginResponse",
        "ErrorResponse",
        "MemoryTickerItem",
        "SessionListItem",
        "AgentListItem",
    ]
    for schema in required:
        assert schema in schemas, f"Missing required schema: {schema}"


def test_openapi_spec_is_up_to_date():
    """Regenerate and compare — ensures docs stay in sync with code."""
    from orchestrator.api_fastapi import spec as generated_spec

    current = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert current == generated_spec, (
        "OpenAPI spec is out of date. Regenerate with: python scripts/generate_openapi.py"
    )
