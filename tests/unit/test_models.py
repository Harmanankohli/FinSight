from datetime import datetime, timezone

from shared.models import (
    InvestmentBrief,
    QuantMetrics,
    QueryContext,
    RAGInsights,
    SentimentIntelligence,
)

_NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_query_context(**kw):
    return QueryContext(
        **{
            "ticker": "AAPL",
            "user_query": "Analyze Apple",
            "user_risk_profile": "medium",
            "portfolio_holdings": ["MSFT"],
            "investment_horizon": "medium_term",
            "session_id": "s1",
            "timestamp": _NOW,
            **kw,
        }
    )


def _make_rag(**kw):
    return RAGInsights(
        **{
            "ticker": "AAPL",
            "revenue_growth_yoy": 0.12,
            "rd_spend_billions": 3.5,
            "forward_guidance": "positive",
            "key_risks": ["competition"],
            "cited_documents": ["10-K 2024"],
            "confidence_score": 0.85,
            **kw,
        }
    )


def _make_quant(**kw):
    return QuantMetrics(
        **{
            "ticker": "AAPL",
            "sharpe_ratio": 1.5,
            "annual_volatility": 0.25,
            "beta": 1.1,
            "var_95_daily": -0.022,
            "portfolio_correlation": {"MSFT": 0.7},
            "quant_signal": "BUY",
            "quant_confidence": 0.78,
            **kw,
        }
    )


def _make_sentiment(**kw):
    return SentimentIntelligence(
        **{
            "ticker": "AAPL",
            "social_sentiment_score": 0.6,
            "analyst_consensus": "bullish",
            "avg_price_target": 200.0,
            "insider_signal": "neutral",
            "narrative": "Strong product cycle",
            "overall_signal": "BUY",
            "confidence_score": 0.75,
            "key_risks": ["macro"],
            "key_catalysts": ["iPhone 17"],
            **kw,
        }
    )


def _make_brief(**kw):
    return InvestmentBrief(
        **{
            "ticker": "AAPL",
            "generated_at": _NOW,
            "query_context": _make_query_context(),
            "rag_insights": _make_rag(),
            "quant_metrics": _make_quant(),
            "sentiment_intelligence": _make_sentiment(),
            "final_recommendation": "BUY",
            "recommendation_rationale": "Strong fundamentals",
            "confidence_score": 0.80,
            "disclaimer": "Not financial advice.",
            **kw,
        }
    )


# ── QueryContext ──────────────────────────────────────────────────────────────


def test_query_context_construction():
    qc = _make_query_context()
    assert qc.ticker == "AAPL"
    assert qc.user_risk_profile == "medium"
    assert "MSFT" in qc.portfolio_holdings


def test_query_context_empty_holdings():
    qc = _make_query_context(portfolio_holdings=[])
    assert qc.portfolio_holdings == []


# ── RAGInsights ───────────────────────────────────────────────────────────────


def test_rag_round_trip():
    rag = _make_rag()
    rag2 = RAGInsights.model_validate(rag.model_dump())
    assert rag2.ticker == rag.ticker
    assert rag2.confidence_score == rag.confidence_score
    assert rag2.key_risks == rag.key_risks


def test_rag_multiple_risks():
    rag = _make_rag(key_risks=["competition", "regulation", "fx_risk"])
    assert len(rag.key_risks) == 3


# ── QuantMetrics ──────────────────────────────────────────────────────────────


def test_quant_optional_fields_default_none():
    quant = _make_quant()
    assert quant.dcf_intrinsic_value is None
    assert quant.stress_test_result is None


def test_quant_with_optional_fields():
    quant = _make_quant(
        dcf_intrinsic_value=185.0,
        stress_test_result={"cvar_95": -0.03, "var_95": -0.025},
    )
    assert quant.dcf_intrinsic_value == 185.0
    assert quant.stress_test_result["cvar_95"] == -0.03


# ── SentimentIntelligence ─────────────────────────────────────────────────────


def test_sentiment_construction():
    s = _make_sentiment()
    assert s.overall_signal == "BUY"
    assert "iPhone 17" in s.key_catalysts


# ── InvestmentBrief ───────────────────────────────────────────────────────────


def test_brief_nested_construction():
    brief = _make_brief()
    assert brief.ticker == "AAPL"
    assert brief.query_context.ticker == "AAPL"
    assert brief.rag_insights.revenue_growth_yoy == 0.12
    assert brief.quant_metrics.sharpe_ratio == 1.5
    assert brief.sentiment_intelligence.analyst_consensus == "bullish"


def test_brief_json_round_trip():
    brief = _make_brief()
    data = brief.model_dump(mode="json")
    brief2 = InvestmentBrief.model_validate(data)
    assert brief2.ticker == brief.ticker
    assert brief2.final_recommendation == brief.final_recommendation
    assert brief2.confidence_score == brief.confidence_score
    assert brief2.generated_at == brief.generated_at


def test_brief_recommendations():
    for rec in ("BUY", "HOLD", "SELL"):
        brief = _make_brief(final_recommendation=rec)
        assert brief.final_recommendation == rec
