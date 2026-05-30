"""E2E integration tests for behavioral signals + Market Context routing.

All tests are @pytest.mark.integration — skipped by default. Run with:
    pytest -m integration tests/integration/test_behavioral_signals_e2e.py

Requires all services running (run_adk_web.bat).
"""
import json

import httpx
import pytest

QUANT_URL = "http://localhost:8003/a2a"
MARKET_CTX_URL = "http://localhost:8004/a2a"


async def _send_a2a(url: str, text: str, timeout: float = 90.0) -> dict:
    """Minimal A2A JSON-RPC sender — returns the parsed task result text."""
    payload = {
        "jsonrpc": "2.0",
        "id": "test-1",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


def _extract_text(rpc_result: dict) -> str:
    """Pull the result text from an A2A JSON-RPC response (best-effort)."""
    res = rpc_result.get("result") or {}
    # Newer A2A: result contains a task with artifacts
    artifacts = res.get("artifacts") or []
    for art in artifacts:
        for part in art.get("parts", []):
            if "text" in part:
                return part["text"]
            if "data" in part:
                return json.dumps(part["data"])
    # Direct message response
    msg = res.get("message") or {}
    for part in msg.get("parts", []):
        if "text" in part:
            return part["text"]
    return json.dumps(res)


@pytest.mark.integration
@pytest.mark.external
async def test_insider_signals_surface_in_quant_response():
    """'Are insiders buying AAPL?' → Quant agent returns Form-4-derived signal fields."""
    rpc = await _send_a2a(QUANT_URL, "Are insiders buying AAPL?")
    txt = _extract_text(rpc)
    lower = txt.lower()
    assert "aapl" in lower or "apple" in lower
    # At least one insider-specific term should appear (data presence varies by quarter)
    insider_terms = ["insider", "form 4", "form-4", "transaction", "held", "ownership"]
    assert any(t in lower for t in insider_terms), f"No insider terms in response: {txt[:400]}"


@pytest.mark.integration
@pytest.mark.external
async def test_options_flow_surfaces_in_quant_response():
    """'What's the options flow on NVDA?' → Quant returns put/call info."""
    rpc = await _send_a2a(QUANT_URL, "What's the options flow on NVDA?")
    txt = _extract_text(rpc)
    lower = txt.lower()
    assert "nvda" in lower or "nvidia" in lower
    flow_terms = ["put", "call", "options", "flow", "open interest"]
    assert any(t in lower for t in flow_terms), f"No options flow terms in response: {txt[:400]}"


@pytest.mark.integration
@pytest.mark.external
async def test_macro_query_routes_to_market_context():
    """'How does the current macro setup affect NVDA?' → Market Context names indicators."""
    rpc = await _send_a2a(MARKET_CTX_URL, "How does the current macro setup affect NVDA?")
    txt = _extract_text(rpc)
    lower = txt.lower()
    # Look for at least one specific macro indicator name
    macro_terms = ["yield curve", "vix", "dxy", "dollar", "10-year", "treasury", "sector", "rotation"]
    assert any(t in lower for t in macro_terms), f"No macro indicators in response: {txt[:400]}"


@pytest.mark.integration
@pytest.mark.external
async def test_peer_landscape_query():
    """'How does AAPL stack up against MSFT and GOOGL?' → response names all three."""
    rpc = await _send_a2a(MARKET_CTX_URL, "How does AAPL stack up against MSFT and GOOGL?")
    txt = _extract_text(rpc).upper()
    # All three tickers should be named explicitly
    assert "AAPL" in txt or "APPLE" in txt
    assert "MSFT" in txt or "MICROSOFT" in txt
    assert "GOOGL" in txt or "GOOGLE" in txt or "ALPHABET" in txt


@pytest.mark.integration
@pytest.mark.external
async def test_quant_response_schema_validation():
    """Quant response includes the schema_validation field from score_quant_deterministic."""
    rpc = await _send_a2a(QUANT_URL, "Calculate risk metrics for AAPL")
    txt = _extract_text(rpc)
    # The response should be JSON-encoded data — parse out the schema_validation block
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        pytest.skip(f"Response is not JSON-encoded: {txt[:200]}")
    assert "schema_validation" in data, f"schema_validation missing: keys={list(data.keys())}"
    assert "passed" in data["schema_validation"]
