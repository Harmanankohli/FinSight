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


def score_confidence(agent_outputs: dict) -> dict:
    quant = agent_outputs.get("quant", {})
    rag = agent_outputs.get("rag", {})
    market = agent_outputs.get("market_context", {})
    analytics = agent_outputs.get("analytics", {})

    quant_conf = _safe_get(quant, "metrics", "quant_confidence", default=0.5)
    if not isinstance(quant_conf, (int, float)):
        quant_conf = 0.5
    rag_conf = rag.get("confidence_score", 0.5)
    if not isinstance(rag_conf, (int, float)):
        rag_conf = 0.5
    market_conf = market.get("confidence_score", 0.5)
    if not isinstance(market_conf, (int, float)):
        market_conf = 0.5
    analytics_conf = analytics.get("analytics_confidence", 0.5)
    if not isinstance(analytics_conf, (int, float)):
        analytics_conf = 0.5

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
    data_quality = filled_fields / total_fields if total_fields > 0 else 0.5

    avg_agent_conf = (quant_conf + rag_conf + market_conf + analytics_conf) / 4
    meta_confidence = 0.4 * avg_agent_conf + 0.3 * agreement + 0.3 * data_quality
    meta_confidence = max(0.0, min(1.0, meta_confidence))

    return {
        "agent_scores": {
            "Quant Analysis Agent": round(quant_conf, 2),
            "Financial RAG Agent": round(rag_conf, 2),
            "Market Context Agent": round(market_conf, 2),
            "Analytics Agent": round(analytics_conf, 2),
        },
        "agreement_score": round(agreement, 2),
        "data_quality_score": round(data_quality, 2),
        "meta_confidence": round(meta_confidence, 2),
    }
