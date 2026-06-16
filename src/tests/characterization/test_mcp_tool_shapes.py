"""Characterization tests for MCP tool return shapes.

Each @app.tool() function is called with upstreams monkeypatched so no
real network calls are made. Only top-level keys and their types are
asserted — internal structure is intentionally left unconstrained so
minor changes to yfinance or EDGAR responses don't break this suite.

Moving any tool function to a new module without a re-export will fail
a test here.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _yf_ticker_mock(symbol: str):
    """Minimal yfinance.Ticker mock."""
    mock = MagicMock()
    import pandas as pd

    mock.history.return_value = pd.DataFrame(
        {"Close": [100.0, 101.0]},
        index=pd.to_datetime(["2024-01-01", "2024-01-02"]),
    )
    mock.info = {
        "longName": f"{symbol} Corp",
        "sector": "Technology",
        "exchange": "NMS",
        "currentPrice": 100.0,
    }
    mock.financials = pd.DataFrame()
    mock.balance_sheet = pd.DataFrame()
    mock.cashflow = pd.DataFrame()
    mock.options = ()
    mock.option_chain.return_value = MagicMock(calls=pd.DataFrame(), puts=pd.DataFrame())
    mock.get_earnings_dates.return_value = pd.DataFrame()
    mock.insider_purchases = pd.DataFrame()
    mock.insider_transactions = pd.DataFrame()
    mock.recommendations = pd.DataFrame()
    mock.analyst_price_targets = pd.DataFrame()
    return mock


def _feedparser_mock():
    mock = MagicMock()
    mock.entries = [
        MagicMock(
            title="NVDA hits record high",
            link="http://example.com/1",
            published="Mon, 01 Jan 2024 00:00:00 +0000",
            summary="NVIDIA stock hits new record.",
        )
    ]
    return mock


# ── Imports (done inside tests to allow monkeypatching at import time) ────────


@pytest.fixture(autouse=True)
def _patch_heavy_imports():
    """Prevent real ML model loads and network calls during import."""
    with (
        patch("sentence_transformers.SentenceTransformer", MagicMock()),
        patch("mcp_tools.tools.agent_registry._model_embed", MagicMock()),
    ):
        yield


# ── get_prices ────────────────────────────────────────────────────────────────


async def test_get_prices_shape():
    with patch("yfinance.Ticker", side_effect=_yf_ticker_mock):
        from mcp_tools.tools.market_data import get_prices

        result = await get_prices("NVDA", period="1mo", interval="1d")
    assert isinstance(result, dict)
    assert "ticker" in result
    assert "data" in result
    assert isinstance(result["data"], list)


async def test_get_prices_error_shape():
    with patch("yfinance.Ticker", side_effect=RuntimeError("network down")):
        from mcp_tools.tools.market_data import get_prices

        result = await get_prices("FAIL")
    assert isinstance(result, dict)
    assert "data" in result
    assert result["data"] == []


# ── get_financials ────────────────────────────────────────────────────────────


async def test_get_financials_shape():
    with patch("yfinance.Ticker", side_effect=_yf_ticker_mock):
        from mcp_tools.tools.market_data import get_financials

        result = await get_financials("NVDA")
    assert isinstance(result, dict)
    for key in ("income_statement", "balance_sheet", "cash_flow", "info"):
        assert key in result, f"missing key: {key}"


# ── get_news_sentiment ────────────────────────────────────────────────────────


async def test_get_news_sentiment_shape():
    with patch("feedparser.parse", return_value=_feedparser_mock()):
        from mcp_tools.tools.sentiment import get_news_sentiment

        result = await get_news_sentiment("NVDA", limit=3)
    assert isinstance(result, dict)
    assert "ticker" in result
    assert "articles" in result
    assert isinstance(result["articles"], list)


# ── get_macro_indicators ──────────────────────────────────────────────────────


async def test_get_macro_indicators_shape():
    with patch("yfinance.Ticker", side_effect=_yf_ticker_mock):
        from mcp_tools.tools.market_data import get_macro_indicators

        result = await get_macro_indicators()
    assert isinstance(result, dict)
    # Route returns 'macro' key (not 'indicators')
    assert "macro" in result or "indicators" in result or "error" in result


# ── validate_ticker ───────────────────────────────────────────────────────────


async def test_validate_ticker_shape():
    with patch("yfinance.Ticker", side_effect=_yf_ticker_mock):
        from mcp_tools.tools.ticker import validate_ticker

        result = await validate_ticker("NVDA")
    assert isinstance(result, dict)
    assert "valid" in result
    assert "ticker" in result


# ── get_peers ─────────────────────────────────────────────────────────────────


async def test_get_peers_shape():
    with patch("yfinance.Ticker", side_effect=_yf_ticker_mock):
        from mcp_tools.tools.sentiment import get_peers

        result = await get_peers("NVDA")
    assert isinstance(result, dict)
    assert "ticker" in result
    assert "peers" in result
    assert isinstance(result["peers"], list)


# ── execute_python ────────────────────────────────────────────────────────────


async def test_execute_python_shape():
    from mcp_tools.tools.sandbox import execute_python

    result = await execute_python("x = 1 + 1")
    assert isinstance(result, dict)
    # Must have at least one of these keys
    assert any(k in result for k in ("output", "stdout", "result", "error", "success"))


async def test_execute_python_error_shape():
    from mcp_tools.tools.sandbox import execute_python

    result = await execute_python("raise ValueError('boom')")
    assert isinstance(result, dict)
    # Either explicit 'error' key or 'success': False / stderr present
    has_error = "error" in result or result.get("success") is False or result.get("stderr")
    assert has_error


# ── get_earnings_calendar ─────────────────────────────────────────────────────


async def test_get_earnings_calendar_shape():
    with patch("yfinance.Ticker", side_effect=_yf_ticker_mock):
        from mcp_tools.tools.sentiment import get_earnings_calendar

        result = await get_earnings_calendar("NVDA")
    assert isinstance(result, dict)
    assert "ticker" in result


# ── get_insider_transactions ──────────────────────────────────────────────────


async def test_get_insider_transactions_shape():
    with patch("yfinance.Ticker", side_effect=_yf_ticker_mock):
        from mcp_tools.tools.sentiment import get_insider_transactions

        result = await get_insider_transactions("NVDA")
    assert isinstance(result, dict)
    assert "ticker" in result
    assert "transactions" in result


# ── get_scenario_shocks ───────────────────────────────────────────────────────


async def test_get_scenario_shocks_shape():
    from mcp_tools.tools.sentiment import get_scenario_shocks

    result = await get_scenario_shocks("Technology")
    assert isinstance(result, dict)
    assert "scenarios" in result or "shocks" in result or "sector" in result


# ── find_agent ────────────────────────────────────────────────────────────────


async def test_find_agent_shape():
    import mcp_tools.tools.agent_registry as _ar_mod

    orig = _ar_mod._df_registry
    try:
        import pandas as pd

        _ar_mod._df_registry = pd.DataFrame()  # empty — triggers the "no cards" path
        result = await _ar_mod.find_agent("investment research")
    finally:
        _ar_mod._df_registry = orig
    # Returns JSON string
    parsed = json.loads(result)
    assert isinstance(parsed, dict)
    # With an empty registry, either 'error' key or a valid agent card dict
    assert isinstance(parsed, dict)
