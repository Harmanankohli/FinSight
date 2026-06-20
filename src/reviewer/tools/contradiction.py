"""Contradiction detection tool that flags conflicting statements across agent outputs."""

import logging

from shared.agent_models import ContradictionFlag

logger = logging.getLogger(__name__)


def _safe_get(d: dict, *keys, default=None):
    """Safely traverse a nested dict returning the value at `keys` or `default` if any key is missing."""
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, {})
        else:
            return default
    return d if d is not None else default


def check_contradictions(agent_outputs: dict) -> list[dict]:
    """Compare outputs from quant, RAG, market context, and analytics agents and return a list of
    ContradictionFlag dicts for any cross-agent conflicts (e.g. BUY rec vs bearish trend, DCF vs macro
    signal, RSI vs recommendation). Deduplicates repeated contradictions by category."""
    seen_categories: set[tuple[str, str]] = set()
    contradictions = []

    def _add(agents, field, description, severity):
        key = (field, description[:80])
        if key not in seen_categories:
            seen_categories.add(key)
            contradictions.append(ContradictionFlag(
                agents=agents,
                field=field,
                description=description,
                severity=severity,
            ).model_dump())

    quant = agent_outputs.get("quant", {})
    rag = agent_outputs.get("rag", {})
    market = agent_outputs.get("market_context", {})
    analytics = agent_outputs.get("analytics", {})

    quant_rec = _safe_get(quant, "recommendation", default="HOLD")
    analytics_trend = _safe_get(analytics, "trend_analysis", "trend_direction", default="neutral")
    rag_sentiment_summary = _safe_get(rag, "summary", default="")
    market_signal = _safe_get(market, "overall_signal", default="neutral")
    quant_signal = _safe_get(quant, "metrics", "quant_signal", default="neutral")
    anomaly_severity = _safe_get(analytics, "anomalies", "severity", default="none")
    quant_confidence = _safe_get(quant, "metrics", "quant_confidence", default=0.5)

    # Quant BUY vs bearish analytics trend
    if quant_rec == "BUY" and analytics_trend == "bearish":
        _add(
            ["Quant Analysis Agent", "Analytics Agent"],
            "recommendation vs trend",
            f"Quant recommends BUY but analytics shows {analytics_trend} trend",
            "high",
        )

    # Quant SELL vs bullish analytics trend
    if quant_rec == "SELL" and analytics_trend == "bullish":
        _add(
            ["Quant Analysis Agent", "Analytics Agent"],
            "recommendation vs trend",
            f"Quant recommends SELL but analytics shows {analytics_trend} trend",
            "high",
        )

    # Quant BUY vs negative RAG sentiment
    if quant_rec == "BUY" and "negative" in rag_sentiment_summary.lower():
        _add(
            ["Quant Analysis Agent", "Financial RAG Agent"],
            "recommendation vs sentiment",
            "Quant recommends BUY but RAG filings summary is negative",
            "medium",
        )

    # Market bearish vs quant bullish signal
    if market_signal == "bearish" and quant_signal == "bullish":
        _add(
            ["Market Context Agent", "Quant Analysis Agent"],
            "macro signal vs quant signal",
            f"Market context is {market_signal} but quant is {quant_signal}",
            "medium",
        )

    # Market bullish vs quant bearish signal
    if market_signal == "bullish" and quant_signal == "bearish":
        _add(
            ["Market Context Agent", "Quant Analysis Agent"],
            "macro signal vs quant signal",
            f"Market context is {market_signal} but quant is {quant_signal}",
            "medium",
        )

    # Anomalies vs high confidence
    if anomaly_severity in ("medium", "high") and quant_confidence and quant_confidence > 0.7:
        _add(
            ["Analytics Agent", "Quant Analysis Agent"],
            "anomaly severity vs confidence",
            f"Analytics reports {anomaly_severity} anomalies but quant confidence is {quant_confidence:.2f}",
            "low",
        )

    # DCF intrinsic value vs market signal
    dcf = quant.get("dcf_valuation") or {}
    dcf_upside = dcf.get("upside_pct")
    if dcf_upside is not None:
        if dcf_upside < -30 and market_signal == "bullish":
            _add(
                ["Quant Analysis Agent", "Market Context Agent"],
                "DCF valuation vs macro signal",
                f"DCF shows {dcf_upside:.1f}% downside but macro context is bullish",
                "medium",
            )
        if dcf_upside > 30 and market_signal == "bearish":
            _add(
                ["Quant Analysis Agent", "Market Context Agent"],
                "DCF valuation vs macro signal",
                f"DCF shows {dcf_upside:.1f}% upside but macro context is bearish",
                "medium",
            )

    # Analytics trend vs market signal
    if analytics_trend == "bullish" and market_signal == "bearish":
        _add(
            ["Analytics Agent", "Market Context Agent"],
            "technical trend vs macro signal",
            "Analytics trend is bullish but macro context is bearish",
            "low",
        )
    if analytics_trend == "bearish" and market_signal == "bullish":
        _add(
            ["Analytics Agent", "Market Context Agent"],
            "technical trend vs macro signal",
            "Analytics trend is bearish but macro context is bullish",
            "low",
        )

    # Quant recommendation vs DCF signal
    if quant_rec == "BUY" and dcf_upside is not None and dcf_upside < -20:
        _add(
            ["Quant Analysis Agent (recommendation)", "Quant Analysis Agent (DCF)"],
            "recommendation vs DCF",
            f"Quant recommends BUY but DCF shows {dcf_upside:.1f}% downside",
            "high",
        )

    # RSI vs recommendation contradiction
    quant_technicals = quant.get("technicals") or {}
    quant_rsi = quant_technicals.get("rsi_14") or quant_technicals.get("rsi")
    if quant_rsi is not None and isinstance(quant_rsi, (int, float)):
        if quant_rec == "BUY" and quant_rsi > 75:
            _add(
                ["Quant Analysis Agent (recommendation)", "Quant Analysis Agent (technicals)"],
                "RSI vs recommendation",
                f"Quant recommends BUY but RSI is overbought at {quant_rsi:.1f}",
                "medium",
            )
        elif quant_rec == "SELL" and quant_rsi < 25:
            _add(
                ["Quant Analysis Agent (recommendation)", "Quant Analysis Agent (technicals)"],
                "RSI vs recommendation",
                f"Quant recommends SELL but RSI is oversold at {quant_rsi:.1f}",
                "medium",
            )

    # DCF vs Monte Carlo median divergence > 40%
    dcf = quant.get("dcf_valuation") or {}
    mc_result = quant.get("monte_carlo") or {}
    dcf_intrinsic = dcf.get("intrinsic_value")
    mc_median = mc_result.get("p50")
    if dcf_intrinsic and mc_median and dcf_intrinsic > 0 and mc_median > 0:
        divergence = abs(dcf_intrinsic - mc_median) / ((dcf_intrinsic + mc_median) / 2)
        if divergence > 0.40:
            _add(
                ["Quant Analysis Agent (DCF)", "Quant Analysis Agent (Monte Carlo)"],
                "DCF vs Monte Carlo divergence",
                f"High model disagreement: DCF=${dcf_intrinsic:.0f}, MC median=${mc_median:.0f} (divergence={divergence:.0%})",
                "medium",
            )

    # Monte Carlo vs recommendation
    mc = quant.get("monte_carlo") or {}
    prob_profit = mc.get("prob_profit")
    if prob_profit is not None:
        if quant_rec == "BUY" and prob_profit < 0.5:
            _add(
                ["Quant Analysis Agent (recommendation)", "Quant Analysis Agent (Monte Carlo)"],
                "recommendation vs Monte Carlo",
                f"Quant recommends BUY but Monte Carlo shows only {prob_profit:.0%} probability of profit",
                "medium",
            )

    for c in contradictions:
        logger.warning(
            "Contradiction [%s]: %s — %s",
            c["severity"],
            c["field"],
            c["description"],
        )
    if not contradictions:
        logger.info("Contradictions: none detected")
    return contradictions
