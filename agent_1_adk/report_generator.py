import logging
from datetime import datetime, timezone

from shared.models import (
    InvestmentBrief,
    QueryContext,
    RAGInsights,
    QuantMetrics,
    SentimentIntelligence,
)

logger = logging.getLogger(__name__)


def synthesize(
    context: QueryContext,
    rag_data: dict | None = None,
    quant_data: dict | None = None,
    sentiment_data: dict | None = None,
) -> InvestmentBrief:
    rag = RAGInsights(
        ticker=context.ticker,
        revenue_growth_yoy=rag_data.get("revenue_growth_yoy", 0.0) if rag_data else 0.0,
        rd_spend_billions=rag_data.get("rd_spend_billions", 0.0) if rag_data else 0.0,
        forward_guidance=rag_data.get("summary", "") if rag_data else "",
        key_risks=rag_data.get("key_risks", []) if rag_data else [],
        cited_documents=rag_data.get("sources", []) if rag_data else [],
        confidence_score=rag_data.get("confidence_score", 0.0) if rag_data else 0.0,
    )

    quant = QuantMetrics(
        ticker=context.ticker,
        sharpe_ratio=quant_data.get("metrics", {}).get("sharpe_ratio", 0.0) if quant_data else 0.0,
        annual_volatility=quant_data.get("metrics", {}).get("annual_volatility", 0.0) if quant_data else 0.0,
        beta=quant_data.get("metrics", {}).get("beta", 0.0) if quant_data else 0.0,
        var_95_daily=quant_data.get("metrics", {}).get("var_95_daily", 0.0) if quant_data else 0.0,
        dcf_intrinsic_value=(quant_data.get("dcf_valuation") or {}).get("intrinsic_value") if quant_data else None,
        stress_test_result=quant_data.get("stress_test") if quant_data else None,
        portfolio_correlation=quant_data.get("correlation_matrix", {}) if quant_data else {},
        quant_signal=quant_data.get("recommendation", "HOLD") if quant_data else "HOLD",
        quant_confidence=quant_data.get("metrics", {}).get("quant_confidence", 0.0) if quant_data else 0.0,
    )

    sentiment = SentimentIntelligence(
        ticker=context.ticker,
        social_sentiment_score=sentiment_data.get("social_sentiment_score", 0.0) if sentiment_data else 0.0,
        analyst_consensus=sentiment_data.get("analyst_consensus", "Hold") if sentiment_data else "Hold",
        avg_price_target=sentiment_data.get("avg_price_target", 0.0) if sentiment_data else 0.0,
        insider_signal=sentiment_data.get("insider_signal", "neutral") if sentiment_data else "neutral",
        narrative=sentiment_data.get("narrative", "") if sentiment_data else "",
        overall_signal=sentiment_data.get("overall_signal", "neutral") if sentiment_data else "neutral",
        confidence_score=sentiment_data.get("confidence_score", 0.0) if sentiment_data else 0.0,
        key_risks=sentiment_data.get("key_risks", []) if sentiment_data else [],
        key_catalysts=sentiment_data.get("key_catalysts", []) if sentiment_data else [],
    )

    scores = [
        rag.confidence_score,
        quant.quant_confidence,
        sentiment.confidence_score,
    ]
    avg_confidence = sum(scores) / len(scores) if scores else 0.0

    signals = [s.upper() for s in [quant.quant_signal, sentiment.overall_signal] if s]
    if "SELL" in signals:
        final_rec = "SELL"
    elif "BUY" in signals and "SELL" not in signals:
        final_rec = "BUY"
    else:
        final_rec = "HOLD"

    rationale_parts = []
    if rag.forward_guidance:
        rationale_parts.append(f"Fundamentals: {rag.forward_guidance[:200]}")
    if quant_data:
        rationale_parts.append(
            f"Quant: Sharpe={quant.sharpe_ratio}, Vol={quant.annual_volatility}, Beta={quant.beta}"
        )
    if sentiment.narrative:
        rationale_parts.append(f"Sentiment: {sentiment.narrative[:200]}")

    return InvestmentBrief(
        ticker=context.ticker,
        generated_at=datetime.now(timezone.utc),
        query_context=context,
        rag_insights=rag,
        quant_metrics=quant,
        sentiment_intelligence=sentiment,
        final_recommendation=final_rec,
        recommendation_rationale=" | ".join(rationale_parts),
        confidence_score=round(avg_confidence, 3),
        disclaimer="This is an AI-generated investment research brief. Not financial advice.",
    )
