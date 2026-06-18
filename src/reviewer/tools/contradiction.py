import logging

logger = logging.getLogger(__name__)


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

    # Quant BUY vs bearish analytics trend
    if quant_rec == "BUY" and analytics_trend == "bearish":
        contradictions.append({
            "agents": ["Quant Analysis Agent", "Analytics Agent"],
            "field": "recommendation vs trend",
            "description": f"Quant recommends BUY but analytics shows {analytics_trend} trend",
            "severity": "high",
        })

    # Quant SELL vs bullish analytics trend
    if quant_rec == "SELL" and analytics_trend == "bullish":
        contradictions.append({
            "agents": ["Quant Analysis Agent", "Analytics Agent"],
            "field": "recommendation vs trend",
            "description": f"Quant recommends SELL but analytics shows {analytics_trend} trend",
            "severity": "high",
        })

    # Quant BUY vs negative RAG sentiment
    if quant_rec == "BUY" and "negative" in rag_sentiment_summary.lower():
        contradictions.append({
            "agents": ["Quant Analysis Agent", "Financial RAG Agent"],
            "field": "recommendation vs sentiment",
            "description": "Quant recommends BUY but RAG filings summary is negative",
            "severity": "medium",
        })

    # Market bearish vs quant bullish signal
    if market_signal == "bearish" and quant_signal == "bullish":
        contradictions.append({
            "agents": ["Market Context Agent", "Quant Analysis Agent"],
            "field": "macro signal vs quant signal",
            "description": f"Market context is {market_signal} but quant is {quant_signal}",
            "severity": "medium",
        })

    # Market bullish vs quant bearish signal
    if market_signal == "bullish" and quant_signal == "bearish":
        contradictions.append({
            "agents": ["Market Context Agent", "Quant Analysis Agent"],
            "field": "macro signal vs quant signal",
            "description": f"Market context is {market_signal} but quant is {quant_signal}",
            "severity": "medium",
        })

    # Anomalies vs high confidence
    if anomaly_severity in ("medium", "high") and quant_confidence and quant_confidence > 0.7:
        contradictions.append({
            "agents": ["Analytics Agent", "Quant Analysis Agent"],
            "field": "anomaly severity vs confidence",
            "description": f"Analytics reports {anomaly_severity} anomalies but quant confidence is {quant_confidence:.2f}",
            "severity": "low",
        })

    # DCF intrinsic value vs market signal
    dcf = quant.get("dcf_valuation") or {}
    dcf_upside = dcf.get("upside_pct")
    if dcf_upside is not None:
        if dcf_upside < -30 and market_signal == "bullish":
            contradictions.append({
                "agents": ["Quant Analysis Agent", "Market Context Agent"],
                "field": "DCF valuation vs macro signal",
                "description": f"DCF shows {dcf_upside:.1f}% downside but macro context is bullish",
                "severity": "medium",
            })
        if dcf_upside > 30 and market_signal == "bearish":
            contradictions.append({
                "agents": ["Quant Analysis Agent", "Market Context Agent"],
                "field": "DCF valuation vs macro signal",
                "description": f"DCF shows {dcf_upside:.1f}% upside but macro context is bearish",
                "severity": "medium",
            })

    # Analytics trend vs market signal
    if analytics_trend == "bullish" and market_signal == "bearish":
        contradictions.append({
            "agents": ["Analytics Agent", "Market Context Agent"],
            "field": "technical trend vs macro signal",
            "description": f"Analytics trend is bullish but macro context is bearish",
            "severity": "low",
        })
    if analytics_trend == "bearish" and market_signal == "bullish":
        contradictions.append({
            "agents": ["Analytics Agent", "Market Context Agent"],
            "field": "technical trend vs macro signal",
            "description": f"Analytics trend is bearish but macro context is bullish",
            "severity": "low",
        })

    # Quant recommendation vs DCF signal
    if quant_rec == "BUY" and dcf_upside is not None and dcf_upside < -20:
        contradictions.append({
            "agents": ["Quant Analysis Agent (recommendation)", "Quant Analysis Agent (DCF)"],
            "field": "recommendation vs DCF",
            "description": f"Quant recommends BUY but DCF shows {dcf_upside:.1f}% downside",
            "severity": "high",
        })

    # Monte Carlo vs recommendation
    mc = quant.get("monte_carlo") or {}
    prob_profit = mc.get("prob_profit")
    if prob_profit is not None:
        if quant_rec == "BUY" and prob_profit < 0.5:
            contradictions.append({
                "agents": ["Quant Analysis Agent (recommendation)", "Quant Analysis Agent (Monte Carlo)"],
                "field": "recommendation vs Monte Carlo",
                "description": f"Quant recommends BUY but Monte Carlo shows only {prob_profit:.0%} probability of profit",
                "severity": "medium",
            })

    for c in contradictions:
        logger.warning(
            "Contradiction [%s]: %s — %s",
            c["severity"], c["field"], c["description"],
        )
    if not contradictions:
        logger.info("Contradictions: none detected")
    return contradictions
