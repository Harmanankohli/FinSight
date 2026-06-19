import logging

logger = logging.getLogger(__name__)


def check_consistency(agent_outputs: dict) -> dict:
    """Cross-validate agent outputs for contradictions before report generation.

    Returns a consistency score (0-1), list of warnings, and contradiction summary.
    """
    warnings = []
    contradictions = []

    quant = agent_outputs.get("quant", {})
    analytics = agent_outputs.get("analytics", {})

    # Check 1: RSI consistency (technical RSI vs any reported market RSI)
    tech = quant.get("technicals") or {}
    rsi = tech.get("rsi_14")
    if rsi is not None:
        if rsi < 30 or rsi > 70:
            warnings.append(f"RSI at extreme level ({rsi:.1f}) — verify signal alignment")

    # Check 2: Valuation consistency (DCF vs Monte Carlo divergence)
    dcf = quant.get("dcf_valuation") or {}
    mc = quant.get("monte_carlo") or {}
    dcf_value = dcf.get("intrinsic_value")
    mc_median = mc.get("p50")
    current_price = dcf.get("current_price")
    if dcf_value and mc_median and current_price and current_price > 0:
        divergence = abs(dcf_value - mc_median) / current_price
        if divergence > 0.40:
            contradictions.append(
                f"Valuation divergence: DCF=${dcf_value:.2f} vs MC median=${mc_median:.2f} "
                f"({divergence:.0%} of price)"
            )

    # Check 3: Recommendation vs valuation consistency
    rec = quant.get("recommendation", "HOLD")
    group_scores = quant.get("metrics", {}).get("signal_scores", {}) or {}
    dcf_score = group_scores.get("dcf_value", 0)
    fund_value_score = group_scores.get("fundamental_value", 0)
    valuation_score = dcf_score + fund_value_score

    if rec == "BUY" and valuation_score < -0.3:
        contradictions.append(
            f"BUY recommendation with negative valuation signal ({valuation_score:+.2f})"
        )
    elif rec == "SELL" and valuation_score > 0.3:
        contradictions.append(
            f"SELL recommendation with positive valuation signal ({valuation_score:+.2f})"
        )

    # Check 4: Technical trend vs recommendation alignment
    trend = tech.get("trend")
    tech_trend_score = group_scores.get("technicals_trend", 0)
    if rec == "BUY" and trend == "downtrend" and tech_trend_score < -0.3:
        contradictions.append(
            f"BUY recommendation during downtrend (tech_trend_score={tech_trend_score:+.2f})"
        )

    # Check 5: Analytics trend vs technical trend conflict
    analytics_trend = (analytics.get("trend_analysis") or {}).get("trend_direction")
    if analytics_trend and trend:
        if analytics_trend == "bullish" and trend == "downtrend":
            contradictions.append(
                "Analytics reports bullish trend but technicals show downtrend"
            )
        elif analytics_trend == "bearish" and trend in ("uptrend", "strong_uptrend"):
            contradictions.append(
                "Analytics reports bearish trend but technicals show uptrend"
            )

    # Compute consistency score: 1.0 = perfect, 0.0 = full contradiction
    total_checks = 5
    issue_count = len(warnings) + len(contradictions)
    consistency_score = max(0.0, 1.0 - (issue_count / total_checks))

    return {
        "consistency_score": round(consistency_score, 2),
        "warnings": warnings,
        "contradictions": contradictions,
        "n_issues": issue_count,
    }
