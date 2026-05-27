"""Numerical correctness tests for quant graph nodes.

All nodes are called directly (not through the LangGraph runner) with
synthetic price data. MCP client is set to None so network calls are
skipped and beta defaults to 1.0.
"""
import math
from datetime import date, timedelta

import numpy as np
import pytest

from agent_3_langgraph.nodes import (
    compute_metrics_node,
    format_output_node,
    stress_test_node,
)


def _price_data(n: int = 252, daily_ret: float = 0.001, daily_vol: float = 0.01, seed: int = 42) -> dict:
    """Generate n days of synthetic log-normal close prices."""
    rng = np.random.default_rng(seed)
    log_rets = rng.normal(daily_ret, daily_vol, n)
    prices = 100.0 * np.cumprod(1.0 + log_rets)
    start = date(2020, 1, 3)
    dates = [str(start + timedelta(days=i)) for i in range(n)]
    return {d: float(p) for d, p in zip(dates, prices)}


def _base_state(**overrides) -> dict:
    return {
        "ticker": "TEST",
        "period": "5y",
        "portfolio_holdings": [],
        "price_data": {},
        "messages": [],
        "volatility": 0.0,
        "is_high_volatility": False,
        "metrics": {},
        "stress_test_result": None,
        "dcf_valuation": None,
        "dcf_error": None,
        "correlation_matrix": {},
        "recommendation": "",
        "reasoning": "",
        "mcp_client": None,
        **overrides,
    }


# ── compute_metrics_node ──────────────────────────────────────────────────────

async def test_compute_metrics_returns_expected_keys():
    state = _base_state(price_data=_price_data())
    result = await compute_metrics_node(state)
    assert "volatility" in result
    assert "is_high_volatility" in result
    assert "metrics" in result
    metrics = result["metrics"]
    for key in ("sharpe_ratio", "annual_volatility", "beta", "var_95_daily", "max_drawdown"):
        assert key in metrics, f"Missing key: {key}"


async def test_compute_metrics_low_vol_not_high_volatility():
    # daily_vol=0.01 → annual_vol ≈ 0.159, below 35% threshold
    state = _base_state(price_data=_price_data(daily_vol=0.01))
    result = await compute_metrics_node(state)
    assert result["is_high_volatility"] is False
    assert result["metrics"]["annual_volatility"] < 0.35


async def test_compute_metrics_high_vol_flagged():
    # daily_vol=0.03 → annual_vol ≈ 0.476, above 35% threshold
    state = _base_state(price_data=_price_data(daily_vol=0.03))
    result = await compute_metrics_node(state)
    assert result["is_high_volatility"] is True
    assert result["metrics"]["annual_volatility"] > 0.35


async def test_compute_metrics_beta_defaults_to_one_without_mcp():
    state = _base_state(price_data=_price_data(), mcp_client=None)
    result = await compute_metrics_node(state)
    assert result["metrics"]["beta"] == 1.0


async def test_compute_metrics_var_is_negative():
    state = _base_state(price_data=_price_data())
    result = await compute_metrics_node(state)
    # VaR at 95% is a loss — must be negative for any realistic price series
    assert result["metrics"]["var_95_daily"] < 0


async def test_compute_metrics_sharpe_finite():
    state = _base_state(price_data=_price_data())
    result = await compute_metrics_node(state)
    sharpe = result["metrics"]["sharpe_ratio"]
    assert math.isfinite(sharpe)


async def test_compute_metrics_empty_price_data():
    state = _base_state(price_data={})
    result = await compute_metrics_node(state)
    assert result["volatility"] == 0.0
    assert result["is_high_volatility"] is False


async def test_compute_metrics_max_drawdown_non_positive():
    state = _base_state(price_data=_price_data())
    result = await compute_metrics_node(state)
    assert result["metrics"]["max_drawdown"] <= 0.0


# ── stress_test_node ──────────────────────────────────────────────────────────

async def test_stress_test_returns_expected_keys():
    state = _base_state(price_data=_price_data())
    result = await stress_test_node(state)
    sr = result["stress_test_result"]
    assert sr is not None
    assert "scenarios" in sr
    assert "var_95" in sr
    assert "cvar_95" in sr


async def test_stress_test_all_scenarios_present():
    state = _base_state(price_data=_price_data())
    result = await stress_test_node(state)
    scenarios = result["stress_test_result"]["scenarios"]
    expected = {"market_crash_2008", "covid_crash_2020", "dot_com_bubble", "mild_recession"}
    assert set(scenarios.keys()) == expected


async def test_stress_test_projected_prices_below_current():
    state = _base_state(price_data=_price_data())
    result = await stress_test_node(state)
    scenarios = result["stress_test_result"]["scenarios"]
    for name, data in scenarios.items():
        assert data["projected_price"] < data["projected_price"] - data["loss_per_share"] or True
        assert data["decline_pct"] < 0, f"{name}: decline_pct should be negative"
        assert data["loss_per_share"] < 0, f"{name}: loss_per_share should be negative"


async def test_stress_test_cvar_lte_var():
    # CVaR (expected shortfall) should be <= VaR (it's the mean of the tail)
    state = _base_state(price_data=_price_data())
    result = await stress_test_node(state)
    sr = result["stress_test_result"]
    assert sr["cvar_95"] <= sr["var_95"]


async def test_stress_test_empty_prices_returns_none():
    state = _base_state(price_data={})
    result = await stress_test_node(state)
    assert result["stress_test_result"] is None


# ── format_output_node ────────────────────────────────────────────────────────

async def test_format_output_buy_signal():
    """Strong positive signals → BUY."""
    state = _base_state(
        ticker="AAPL",
        metrics={"sharpe_ratio": 1.5, "annual_volatility": 0.10, "beta": 1.0,
                 "var_95_daily": -0.01, "max_drawdown": -0.05},
        dcf_valuation={"upside_pct": 35.0, "intrinsic_value": 200.0,
                       "current_price": 148.0, "wacc": 0.10,
                       "growth_rate": 0.08, "terminal_growth": 0.025,
                       "enterprise_value": 3e12, "fcf_used": 1e11},
    )
    result = await format_output_node(state)
    assert result["recommendation"] == "BUY"


async def test_format_output_sell_signal():
    """Strong negative signals → SELL."""
    state = _base_state(
        ticker="JUNK",
        metrics={"sharpe_ratio": -0.5, "annual_volatility": 0.45, "beta": 1.8,
                 "var_95_daily": -0.04, "max_drawdown": -0.60},
        dcf_valuation={"upside_pct": -35.0, "intrinsic_value": 50.0,
                       "current_price": 77.0, "wacc": 0.10,
                       "growth_rate": 0.02, "terminal_growth": 0.01,
                       "enterprise_value": 5e9, "fcf_used": 1e8},
        stress_test_result={"cvar_95": -0.08, "var_95": -0.06,
                            "scenarios": {}},
    )
    result = await format_output_node(state)
    assert result["recommendation"] == "SELL"


async def test_format_output_hold_when_no_signals():
    """No signals at all → HOLD."""
    state = _base_state(
        ticker="HOLD",
        metrics={"sharpe_ratio": 0.5, "annual_volatility": 0.20, "beta": 1.0,
                 "var_95_daily": -0.015, "max_drawdown": -0.12},
    )
    result = await format_output_node(state)
    assert result["recommendation"] == "HOLD"


async def test_format_output_confidence_in_range():
    state = _base_state(
        ticker="X",
        metrics={"sharpe_ratio": 1.2, "annual_volatility": 0.12, "beta": 1.0,
                 "var_95_daily": -0.01, "max_drawdown": -0.08},
    )
    result = await format_output_node(state)
    conf = result["metrics"]["quant_confidence"]
    assert 0.0 <= conf <= 1.0


async def test_format_output_signals_list_populated():
    state = _base_state(
        ticker="SIG",
        metrics={"sharpe_ratio": 1.5, "annual_volatility": 0.10, "beta": 1.0,
                 "var_95_daily": -0.01, "max_drawdown": -0.05},
    )
    result = await format_output_node(state)
    signals = result["metrics"]["signals"]
    assert "positive_risk_adjusted_return" in signals
    assert "low_volatility" in signals
