import pytest
from unittest.mock import patch

from agent_3_langgraph.graph import QuantAnalysisGraph
from agent_3_langgraph.state import QuantAnalysisState


@pytest.fixture
def graph():
    return QuantAnalysisGraph()


@pytest.mark.asyncio
async def test_graph_routes_to_stress_test_on_high_volatility(graph):
    state: QuantAnalysisState = {
        "ticker": "NVDA",
        "period": "5y",
        "portfolio_holdings": [],
        "price_data": {"2024-01-01": 100.0, "2024-06-01": 150.0},
        "messages": [],
        "volatility": 0.45,
        "is_high_volatility": True,
        "metrics": {"sharpe_ratio": 0.5, "annual_volatility": 0.45, "beta": 1.2, "var_95_daily": -0.02},
        "stress_test_result": None,
        "dcf_valuation": None,
        "correlation_matrix": {},
        "recommendation": "",
        "reasoning": "",
    }

    from agent_3_langgraph.graph import _route_on_volatility
    route = _route_on_volatility(state)
    assert route == "stress_test"


@pytest.mark.asyncio
async def test_graph_routes_to_dcf_on_low_volatility(graph):
    state: QuantAnalysisState = {
        "ticker": "AAPL",
        "period": "5y",
        "portfolio_holdings": [],
        "price_data": {"2024-01-01": 200.0, "2024-06-01": 210.0},
        "messages": [],
        "volatility": 0.18,
        "is_high_volatility": False,
        "metrics": {"sharpe_ratio": 1.5, "annual_volatility": 0.18, "beta": 0.9, "var_95_daily": -0.01},
        "stress_test_result": None,
        "dcf_valuation": None,
        "correlation_matrix": {},
        "recommendation": "",
        "reasoning": "",
    }

    from agent_3_langgraph.graph import _route_on_volatility
    route = _route_on_volatility(state)
    assert route == "dcf"


@pytest.mark.asyncio
async def test_full_graph_execution(graph):
    with patch("yfinance.Ticker") as mock_ticker:
        mock_instance = mock_ticker.return_value
        mock_instance.history.return_value = __import__("pandas").DataFrame(
            {"Close": [100, 101, 102, 103, 104]},
            index=__import__("pandas").date_range("2024-01-01", periods=5, freq="D"),
        )

        result = await graph.run("TEST", period="5d", portfolio_holdings=[])

        assert "ticker" in result
        assert "recommendation" in result
        assert result["ticker"] == "TEST"


@pytest.mark.asyncio
async def test_graph_handles_empty_data(graph):
    result = await graph.run("INVALID", period="1d", portfolio_holdings=[])
    assert result["ticker"] == "INVALID"
    assert result["recommendation"] in ("BUY", "HOLD", "SELL")
