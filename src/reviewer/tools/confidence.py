import logging

logger = logging.getLogger(__name__)


def _safe_get(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, {})
        else:
            return default
    return d if d is not None else default


def _signal_to_direction(agent_outputs: dict, agent_key: str) -> str | None:
    agent = agent_outputs.get(agent_key, {})
    if agent_key == "quant":
        rec = agent.get("recommendation", "")
        if rec == "BUY":
            return "bullish"
        elif rec == "SELL":
            return "bearish"
        elif rec == "HOLD":
            return "neutral"
        sig = _safe_get(agent, "metrics", "quant_signal", default="")
        if sig:
            return sig
        return None
    elif agent_key == "rag":
        return None
    elif agent_key == "market_context":
        return agent.get("overall_signal", "neutral")
    elif agent_key == "analytics":
        return agent.get("analytics_signal", "neutral")
    return None


def _derive_quant_confidence(quant: dict) -> float:
    """Derive a confidence score from quant data when quant_confidence is missing."""
    metrics = quant.get("metrics") or {}
    explicit = metrics.get("quant_confidence")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return explicit

    signals = []
    sharpe = metrics.get("sharpe_ratio")
    if isinstance(sharpe, (int, float)) and sharpe != 0:
        signals.append(min(abs(sharpe) / 2.0, 1.0))
    dcf = quant.get("dcf_valuation") or {}
    if dcf.get("intrinsic_value") is not None:
        signals.append(0.6)
    mc = quant.get("monte_carlo") or {}
    if mc.get("p50") is not None:
        signals.append(0.7)
    if quant.get("fundamentals"):
        signals.append(0.5)
    if quant.get("technicals"):
        signals.append(0.5)
    return sum(signals) / len(signals) if signals else 0.5


def _derive_rag_confidence(rag: dict) -> float:
    """Derive a confidence score from RAG data."""
    explicit = rag.get("confidence_score")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return explicit

    score = 0.3
    if rag.get("summary"):
        score += 0.2
    sources = rag.get("sources") or []
    if sources:
        score += min(len(sources) * 0.1, 0.3)
    return min(score, 1.0)


def _derive_market_confidence(market: dict) -> float:
    """Derive a confidence score from market context data."""
    explicit = market.get("confidence_score")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return explicit

    score = 0.3
    if market.get("narrative"):
        score += 0.2
    if market.get("overall_signal") and market["overall_signal"] != "neutral":
        score += 0.1
    if market.get("key_tailwinds"):
        score += 0.1
    if market.get("key_headwinds"):
        score += 0.1
    if market.get("macro_regime"):
        score += 0.1
    return min(score, 1.0)


def _derive_analytics_confidence(analytics: dict) -> float:
    """Derive a confidence score from analytics data."""
    explicit = analytics.get("analytics_confidence")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return explicit

    score = 0.3
    if analytics.get("trend_analysis"):
        score += 0.2
    if analytics.get("forecast"):
        score += 0.2
    if analytics.get("statistical_summary"):
        score += 0.1
    if analytics.get("charts"):
        score += 0.1
    return min(score, 1.0)


def score_confidence(agent_outputs: dict) -> dict:
    quant = agent_outputs.get("quant", {})
    rag = agent_outputs.get("rag", {})
    market = agent_outputs.get("market_context", {})
    analytics = agent_outputs.get("analytics", {})

    quant_conf = _derive_quant_confidence(quant) if quant else 0.0
    rag_conf = _derive_rag_confidence(rag) if rag else 0.0
    market_conf = _derive_market_confidence(market) if market else 0.0
    analytics_conf = _derive_analytics_confidence(analytics) if analytics else 0.0

    directions = []
    for key in ("quant", "market_context", "analytics"):
        d = _signal_to_direction(agent_outputs, key)
        if d:
            directions.append(d)

    if directions:
        bullish = directions.count("bullish")
        bearish = directions.count("bearish")
        neutral = directions.count("neutral")
        total = len(directions)
        agreement = max(bullish, bearish, neutral) / total if total > 0 else 0
    else:
        agreement = 0.0

    total_fields = 0
    filled_fields = 0
    for agent_key, agent_data in [("quant", quant), ("rag", rag), ("market_context", market), ("analytics", analytics)]:
        if isinstance(agent_data, dict):
            for k in agent_data:
                total_fields += 1
                v = agent_data[k]
                if v is not None and (not isinstance(v, (list, dict)) or len(v) > 0):
                    filled_fields += 1
    data_quality = filled_fields / total_fields if total_fields > 0 else 0.0

    avg_agent_conf = (quant_conf + rag_conf + market_conf + analytics_conf) / 4
    meta_confidence = 0.4 * avg_agent_conf + 0.3 * agreement + 0.3 * data_quality
    meta_confidence = max(0.0, min(1.0, meta_confidence))

    logger.info(
        "Confidence: meta=%.2f (agreement=%.2f, quality=%.2f, agents=[quant=%.2f, rag=%.2f, market=%.2f, analytics=%.2f])",
        meta_confidence, agreement, data_quality, quant_conf, rag_conf, market_conf, analytics_conf,
    )
    return {
        "agent_scores": {
            "quant": round(quant_conf, 2),
            "rag": round(rag_conf, 2),
            "market_context": round(market_conf, 2),
            "analytics": round(analytics_conf, 2),
        },
        "agreement_score": round(agreement, 2),
        "data_quality_score": round(data_quality, 2),
        "meta_confidence": round(meta_confidence, 2),
    }
