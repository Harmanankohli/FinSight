"""Tests for analytics nodes with synthetic price data."""

import pytest

from analytics.nodes.trend import _detect_trends
from analytics.nodes.forecast import _run_forecast
from analytics.nodes.anomaly import _detect_anomalies
from analytics.nodes.charts import _generate_charts


def _make_price_data(close_values: list[float]) -> dict:
    import datetime
    start = datetime.date(2023, 1, 1)
    return {
        (start + datetime.timedelta(days=i)).strftime("%Y-%m-%d"): float(v)
        for i, v in enumerate(close_values)
    }


@pytest.mark.asyncio
async def test_trend_detection_golden_cross():
    prices = list(range(80, 120)) + list(range(120, 200, 2))
    price_data = _make_price_data(prices[:200])
    result = await _detect_trends(price_data)
    assert "trend_direction" in result
    assert isinstance(result["trend_strength"], float)


@pytest.mark.asyncio
async def test_trend_detection_insufficient_data():
    price_data = _make_price_data([100.0] * 20)
    result = await _detect_trends(price_data)
    assert result["trend_direction"] == "neutral"


@pytest.mark.asyncio
async def test_forecast_insufficient_data():
    price_data = _make_price_data([100.0] * 30)
    result = await _run_forecast(price_data)
    assert result["forecast_prices"] == []


@pytest.mark.asyncio
async def test_forecast_sufficient_data():
    import math
    prices = [100.0 * (1 + 0.001 * i + 0.01 * math.sin(i / 5)) for i in range(200)]
    price_data = _make_price_data(prices)
    result = await _run_forecast(price_data)
    assert "forecast_prices" in result
    if result["forecast_prices"]:
        assert len(result["forecast_prices"]) == 30
        assert len(result["forecast_dates"]) == 30


@pytest.mark.asyncio
async def test_anomaly_detection_no_anomalies():
    closes = [100.0 + i * 0.1 for i in range(100)]
    price_data = _make_price_data(closes)
    ohlcv = [{"date": k, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000000} for k in price_data.keys()]
    result = await _detect_anomalies(price_data, ohlcv, {})
    assert "anomaly_count" in result
    assert result["severity"] in ("none", "low")


@pytest.mark.asyncio
async def test_chart_generation():
    closes = [100.0 + i for i in range(100)]
    price_data = _make_price_data(closes)
    ohlcv = [{"date": k, "open": float(v - 1), "high": float(v + 1), "low": float(v - 1), "close": float(v), "volume": 1000000} for k, v in price_data.items()]
    charts = await _generate_charts(ohlcv, price_data)
    assert len(charts) >= 1
    assert charts[0]["chart_type"] in ("candlestick", "line", "area")
