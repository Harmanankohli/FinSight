"""Unit tests for shared/agent_models.

Validates schema serialisation, field defaults, and A2A contract compliance.
"""

from datetime import UTC, datetime

from shared.models import (
    InvestmentBrief,
    MarketContext,
    QuantMetrics,
    QueryContext,
    RAGInsights,
)

_NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)


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
            "summary": "Strong revenue growth driven by services.",
            "sources": [{"title": "10-K 2024"}],
            "relevance_scores": [0.92],
            "confidence_score": 0.85,
            "context_texts": ["Revenue grew 12% YoY"],
            **kw,
        }
    )


def _make_quant(**kw):
    return QuantMetrics(
        **{
            "ticker": "AAPL",
            "recommendation": "BUY",
            "reasoning": "Strong risk-adjusted returns.",
            **kw,
        }
    )


def _make_market_context(**kw):
    return MarketContext(
        **{
            "ticker": "AAPL",
            "narrative": "Strong product cycle",
            "overall_signal": "BUY",
            "confidence_score": 0.75,
            "key_headwinds": ["macro"],
            "key_tailwinds": ["iPhone 17"],
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
            "market_context": _make_market_context(),
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
    assert rag2.context_texts == rag.context_texts


def test_rag_multiple_context_texts():
    rag = _make_rag(context_texts=["text1", "text2", "text3"])
    assert len(rag.context_texts) == 3


# ── QuantMetrics ──────────────────────────────────────────────────────────────


def test_quant_optional_fields_default_none():
    quant = _make_quant()
    assert quant.dcf_valuation is None
    assert quant.stress_test is None


def test_quant_with_optional_fields():
    quant = _make_quant(
        dcf_valuation={
            "intrinsic_value": 185.0,
            "current_price": 170.0,
            "upside_pct": 8.8,
            "wacc": 0.09,
            "growth_rate": 0.05,
            "terminal_growth": 0.025,
            "enterprise_value": 2.5e12,
            "fcf_used": 1.0e11,
        },
        stress_test={"cvar_95": -0.03, "var_95": -0.025},
    )
    assert quant.dcf_valuation.intrinsic_value == 185.0
    assert quant.stress_test.cvar_95 == -0.03


# ── MarketContext ─────────────────────────────────────────────────────


def test_market_context_construction():
    mc = _make_market_context()
    assert mc.overall_signal == "BUY"
    assert "iPhone 17" in mc.key_tailwinds


# ── InvestmentBrief ───────────────────────────────────────────────────────────


def test_brief_nested_construction():
    brief = _make_brief()
    assert brief.ticker == "AAPL"
    assert brief.query_context.ticker == "AAPL"
    assert brief.rag_insights.confidence_score == 0.85
    assert brief.quant_metrics.recommendation == "BUY"
    assert brief.market_context.overall_signal == "BUY"


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
