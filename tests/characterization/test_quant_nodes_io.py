"""Characterization tests for LangGraph quant node state I/O.

Each node is called directly (bypassing the graph runner) with a
synthetic state and a stubbed MCP client. Tests assert output keys
and types — not precise numeric values — so they serve as a safety
net for structural refactoring (WP 1.3).
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _price_data(n: int = 252, seed: int = 42) -> dict:
    rng = np.random.default_rng(seed)
    log_rets = rng.normal(0.001, 0.015, n)
    prices = 100.0 * np.cumprod(1.0 + log_rets)
    start = date(2023, 1, 3)
    return {str(start + timedelta(days=i)): float(p) for i, p in enumerate(prices)}


def _stub_mcp() -> MagicMock:
    """MCP client that returns plausible-shaped responses for every tool call."""
    mcp = MagicMock()
    mcp.call_tool = AsyncMock(return_value=MagicMock(
        content=[MagicMock(text='{"data": [], "error": null}')]
    ))
    return mcp


def _base_state(**overrides) -> dict:
    return {
        "ticker": "NVDA",
        "period": "1y",
        "portfolio_holdings": [],
        "price_data": {},
        "messages": [],
        "volatility": 0.0,
        "is_high_volatility": False,
        "metrics": {},
        "stress_test_result": None,
        "dcf_valuation": None,
        "dcf_error": None,
        "fundamental_data": None,
        "technical_data": None,
        "peer_comparison": None,
        "options_data": None,
        "insider_data": None,
        "analyst_data": None,
        "correlation_matrix": {},
        "recommendation": "",
        "reasoning": "",
        "mcp_client": _stub_mcp(),
        **overrides,
    }


# ── compute_metrics_node ──────────────────────────────────────────────────────

async def test_compute_metrics_output_keys():
    from agent_3_langgraph.nodes import compute_metrics_node

    state = _base_state(price_data=_price_data())
    result = await compute_metrics_node(state)

    assert "volatility" in result
    assert "is_high_volatility" in result
    assert "metrics" in result
    assert isinstance(result["volatility"], float)
    assert isinstance(result["is_high_volatility"], bool)
    for key in ("sharpe_ratio", "annual_volatility", "beta", "var_95_daily", "max_drawdown"):
        assert key in result["metrics"], f"metrics missing: {key}"


async def test_compute_metrics_empty_prices():
    from agent_3_langgraph.nodes import compute_metrics_node

    state = _base_state(price_data={})
    result = await compute_metrics_node(state)
    assert "metrics" in result


# ── stress_test_node ──────────────────────────────────────────────────────────

async def test_stress_test_output_keys():
    from agent_3_langgraph.nodes import stress_test_node

    pd = _price_data()
    metrics = {"annual_volatility": 0.35, "max_drawdown": -0.22, "sharpe_ratio": 1.2,
                "beta": 1.5, "var_95_daily": -0.025}
    state = _base_state(price_data=pd, metrics=metrics, volatility=0.35, is_high_volatility=True)
    result = await stress_test_node(state)

    assert "stress_test_result" in result
    if result["stress_test_result"] is not None:
        sr = result["stress_test_result"]
        assert isinstance(sr, dict)


# ── format_output_node ────────────────────────────────────────────────────────

async def test_format_output_keys():
    from agent_3_langgraph.nodes import format_output_node

    metrics = {"annual_volatility": 0.35, "sharpe_ratio": 1.5, "beta": 1.2,
                "var_95_daily": -0.02, "max_drawdown": -0.25}
    state = _base_state(
        price_data=_price_data(),
        metrics=metrics,
        recommendation="BUY",
        reasoning="Strong fundamentals.",
        volatility=0.35,
        is_high_volatility=True,
    )
    result = await format_output_node(state)

    # format_output_node populates recommendation / reasoning / metrics
    assert isinstance(result, dict)
    assert "recommendation" in result or "reasoning" in result or "metrics" in result


# ── fetch_price_data_node ─────────────────────────────────────────────────────

async def test_fetch_price_data_returns_price_data_key():
    from agent_3_langgraph.nodes import fetch_price_data_node
    import json

    price_payload = {
        "ticker": "NVDA",
        "data": [{"Close": 100.0, "Date": "2024-01-01"}],
    }
    mcp = _stub_mcp()
    mcp.call_tool = AsyncMock(return_value=MagicMock(
        content=[MagicMock(text=json.dumps(price_payload))]
    ))
    state = _base_state(mcp_client=mcp)
    result = await fetch_price_data_node(state)

    assert "price_data" in result
    assert isinstance(result["price_data"], dict)


# ── dcf_valuation_node ────────────────────────────────────────────────────────

async def test_dcf_valuation_output_keys():
    from agent_3_langgraph.nodes import dcf_valuation_node
    import json

    financials_payload = {
        "income_statement": {},
        "balance_sheet": {},
        "cash_flow": {},
        "info": {"currentPrice": 500.0, "sharesOutstanding": 2_440_000_000},
    }
    mcp = _stub_mcp()
    mcp.call_tool = AsyncMock(return_value=MagicMock(
        content=[MagicMock(text=json.dumps(financials_payload))]
    ))
    state = _base_state(mcp_client=mcp, price_data=_price_data())
    result = await dcf_valuation_node(state)

    assert "dcf_valuation" in result or "dcf_error" in result


# ── peer_comparison_node ──────────────────────────────────────────────────────

async def test_peer_comparison_output_keys():
    from agent_3_langgraph.nodes import peer_comparison_node
    import json

    peers_payload = {"ticker": "NVDA", "peers": ["AMD", "INTC"]}
    peer_fin = {"income_statement": {}, "balance_sheet": {}, "cash_flow": {}, "info": {}}
    call_count = 0

    async def _side_effect(tool_name, arguments):
        nonlocal call_count
        call_count += 1
        if tool_name == "get_peers":
            return MagicMock(content=[MagicMock(text=json.dumps(peers_payload))])
        return MagicMock(content=[MagicMock(text=json.dumps(peer_fin))])

    mcp = _stub_mcp()
    mcp.call_tool = _side_effect
    state = _base_state(mcp_client=mcp)
    result = await peer_comparison_node(state)

    assert "peer_comparison" in result
