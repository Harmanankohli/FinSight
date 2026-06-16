def _safe_get(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, {})
        else:
            return default
    return d if d is not None else default


def validate_recommendation(agent_outputs: dict) -> dict:
    quant = agent_outputs.get("quant", {})
    rag = agent_outputs.get("rag", {})
    market = agent_outputs.get("market_context", {})
    analytics = agent_outputs.get("analytics", {})

    recommendation = quant.get("recommendation", "HOLD")

    supporting = []
    contradicting = []

    dcf = quant.get("dcf_valuation") or {}
    upside = dcf.get("upside_pct")
    if upside is not None:
        if upside > 20:
            supporting.append(f"DCF upside of {upside:.1f}%")
        elif upside < -10:
            contradicting.append(f"DCF downside of {upside:.1f}%")

    trend = _safe_get(analytics, "trend_analysis", "trend_direction", default="neutral")
    if trend == "bullish":
        supporting.append("Bullish technical trend")
    elif trend == "bearish":
        contradicting.append("Bearish technical trend")

    ma_cross = _safe_get(analytics, "trend_analysis", "ma_crossover_signal", default=None)
    if ma_cross == "golden_cross":
        supporting.append("Golden cross detected")
    elif ma_cross == "death_cross":
        contradicting.append("Death cross detected")

    market_signal = market.get("overall_signal", "neutral")
    if market_signal == "bullish":
        supporting.append("Bullish macro context")
    elif market_signal == "bearish":
        contradicting.append("Bearish macro context")

    rag_summary = rag.get("summary", "")
    if rag_summary:
        positive_words = ["growth", "profit", "positive", "strong", "beat", "exceed"]
        negative_words = ["decline", "loss", "negative", "weak", "miss", "risk"]
        if any(w in rag_summary.lower() for w in positive_words):
            supporting.append("Positive RAG filing signals")
        if any(w in rag_summary.lower() for w in negative_words):
            contradicting.append("Negative RAG filing signals")

    quant_signal = _safe_get(quant, "metrics", "quant_signal", default="neutral")
    if quant_signal == "bullish":
        supporting.append("Bullish quant signal")
    elif quant_signal == "bearish":
        contradicting.append("Bearish quant signal")

    anomaly_severity = _safe_get(analytics, "anomalies", "severity", default="none")
    if anomaly_severity in ("medium", "high"):
        contradicting.append(f"Anomaly severity: {anomaly_severity}")

    if recommendation == "BUY":
        evidence_supports = len(supporting) > len(contradicting)
    elif recommendation == "SELL":
        evidence_supports = len(contradicting) > len(supporting)
    else:
        evidence_supports = True

    total_evidence = max(len(supporting), len(contradicting), 1)
    ratio = max(len(supporting), len(contradicting)) / total_evidence
    if ratio > 3:
        strength = "strong"
    elif ratio > 1.5:
        strength = "moderate"
    else:
        strength = "weak"

    return {
        "recommendation": recommendation,
        "evidence_supports": evidence_supports,
        "supporting_evidence": supporting,
        "contradicting_evidence": contradicting,
        "evidence_strength": strength,
    }
