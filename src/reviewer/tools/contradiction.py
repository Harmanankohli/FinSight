def _safe_get(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, {})
        else:
            return default
    return d if d is not None else default


def check_contradictions(agent_outputs: dict) -> list[dict]:
    contradictions = []

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

    if quant_rec == "BUY" and analytics_trend == "bearish":
        contradictions.append({
            "agents": ["Quant Analysis Agent", "Analytics Agent"],
            "field": "recommendation vs trend",
            "description": f"Quant recommends BUY but analytics shows {analytics_trend} trend",
            "severity": "high",
        })

    if quant_rec == "BUY" and "negative" in rag_sentiment_summary.lower():
        contradictions.append({
            "agents": ["Quant Analysis Agent", "Financial RAG Agent"],
            "field": "recommendation vs sentiment",
            "description": "Quant recommends BUY but RAG filings summary is negative",
            "severity": "medium",
        })

    if market_signal == "bearish" and quant_signal == "bullish":
        contradictions.append({
            "agents": ["Market Context Agent", "Quant Analysis Agent"],
            "field": "macro signal vs quant signal",
            "description": f"Market context is {market_signal} but quant is {quant_signal}",
            "severity": "medium",
        })

    if anomaly_severity in ("medium", "high") and quant_confidence and quant_confidence > 0.7:
        contradictions.append({
            "agents": ["Analytics Agent", "Quant Analysis Agent"],
            "field": "anomaly severity vs confidence",
            "description": f"Analytics reports {anomaly_severity} anomalies but quant confidence is {quant_confidence:.2f}",
            "severity": "low",
        })

    return [c for c in contradictions if c]
