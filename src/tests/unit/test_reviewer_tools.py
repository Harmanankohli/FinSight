"""Tests for reviewer tool functions with mock agent outputs."""

from reviewer.tools.contradiction import check_contradictions
from reviewer.tools.verification import verify_sources
from reviewer.tools.confidence import score_confidence
from reviewer.tools.validation import validate_recommendation


def _make_mock_outputs(**overrides) -> dict:
    base = {
        "quant": {
            "recommendation": "BUY",
            "metrics": {"quant_confidence": 0.8, "quant_signal": "bullish", "sharpe_ratio": 1.5, "var_95_daily": 0.02},
            "dcf_valuation": {"intrinsic_value": 200, "current_price": 150, "upside_pct": 33.33},
        },
        "rag": {"confidence_score": 0.85, "sources": ["10-K"], "summary": "Strong growth and positive outlook"},
        "market_context": {"overall_signal": "bullish", "confidence_score": 0.7},
        "analytics": {
            "analytics_confidence": 0.75,
            "trend_analysis": {"trend_direction": "bullish", "ma_crossover_signal": None},
            "forecast": {"forecast_prices": [160, 165], "forecast_dates": ["2025-02-01", "2025-02-02"]},
            "charts": [{"chart_type": "candlestick", "datasets": [{}]}],
            "anomalies": {"severity": "low", "price_anomalies": [], "volume_anomalies": [], "fundamental_anomalies": []},
        },
    }
    result = base.copy()
    result.update(overrides)
    for k, v in overrides.items():
        if k in base:
            result[k] = {**base[k], **(v if isinstance(v, dict) else {})}
    return result


def test_check_contradictions_no_contradictions():
    outputs = _make_mock_outputs()
    result = check_contradictions(outputs)
    assert isinstance(result, list)


def test_check_contradictions_quant_buy_bearish_trend():
    outputs = _make_mock_outputs()
    outputs["analytics"]["trend_analysis"]["trend_direction"] = "bearish"
    result = check_contradictions(outputs)
    assert len(result) >= 1
    assert any("trend" in c["field"] for c in result)


def test_check_contradictions_anomaly_high():
    outputs = _make_mock_outputs()
    outputs["analytics"]["anomalies"]["severity"] = "high"
    result = check_contradictions(outputs)
    assert len(result) >= 1


def test_verify_sources_quant_dcf():
    outputs = _make_mock_outputs()
    result = verify_sources(outputs)
    assert isinstance(result, list)
    assert any(v["agent_name"] == "Quant Analysis Agent" for v in result)


def test_verify_sources_rag_missing_sources():
    outputs = _make_mock_outputs()
    outputs["rag"]["sources"] = []
    result = verify_sources(outputs)
    rag_ver = [v for v in result if v["agent_name"] == "Financial RAG Agent"]
    if rag_ver:
        assert rag_ver[0]["unverified_claims"]


def test_score_confidence():
    outputs = _make_mock_outputs()
    result = score_confidence(outputs)
    assert "meta_confidence" in result
    assert "agent_scores" in result
    assert "agreement_score" in result
    assert 0 <= result["meta_confidence"] <= 1
    assert len(result["agent_scores"]) == 4


def test_score_confidence_ranges():
    outputs = _make_mock_outputs()
    result = score_confidence(outputs)
    for v in result["agent_scores"].values():
        assert 0 <= v <= 1
    assert 0 <= result["agreement_score"] <= 1
    assert 0 <= result["data_quality_score"] <= 1


def test_validate_recommendation():
    outputs = _make_mock_outputs()
    result = validate_recommendation(outputs)
    assert "recommendation" in result
    assert "evidence_supports" in result
    assert "evidence_strength" in result
    assert result["evidence_strength"] in ("weak", "moderate", "strong")


def test_validate_recommendation_bearish():
    outputs = _make_mock_outputs()
    outputs["analytics"]["trend_analysis"]["trend_direction"] = "bearish"
    outputs["market_context"]["overall_signal"] = "bearish"
    outputs["analytics"]["trend_analysis"]["ma_crossover_signal"] = "death_cross"
    result = validate_recommendation(outputs)
    assert result["contradicting_evidence"]
