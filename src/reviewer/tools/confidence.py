"""Confidence scoring tool that evaluates agent outputs for reliability."""

import logging
import re

from shared.agent_models import AgentScores, ConfidenceBreakdown

logger = logging.getLogger(__name__)

_ERROR_PATTERNS = re.compile(
    r"error|failed|exception|traceback|could not|unable to|no data|timed?\s*out",
    re.IGNORECASE,
)


def _looks_like_error(text: str) -> bool:
    if not text or len(text) < 10:
        return False
    return bool(_ERROR_PATTERNS.search(text[:300]))


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
    """Derive a confidence score weighted by data freshness, source quality,
    and signal agreement."""
    reasoning = quant.get("reasoning") or ""
    if _looks_like_error(reasoning) and not quant.get("metrics"):
        logger.warning(
            "Quant reasoning looks like an error with no metrics, assigning near-zero confidence"
        )
        return 0.05

    metrics = quant.get("metrics") or {}
    explicit = metrics.get("quant_confidence")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return explicit

    # Data freshness: score by how many data sources are present (proxy for data coverage)
    freshness_components = []
    if quant.get("fundamentals"):
        freshness_components.append(1.0)
    if quant.get("technicals"):
        freshness_components.append(1.0)
    if quant.get("monte_carlo") and quant["monte_carlo"].get("p50") is not None:
        freshness_components.append(1.0)
    if quant.get("positioning"):
        freshness_components.append(0.8)
    freshness = (
        sum(freshness_components) / max(len(freshness_components), 1)
        if freshness_components
        else 0.3
    )

    # Signal agreement: check if sub-signals align with recommendation
    rec = quant.get("recommendation", "HOLD")
    agreement_signals = []
    sharpe = metrics.get("sharpe_ratio")
    if isinstance(sharpe, (int, float)) and sharpe != 0:
        agreement_signals.append("bullish" if sharpe > 0 else "bearish")
    signal_scores = metrics.get("signal_scores") or {}
    if signal_scores:
        total_score = sum(signal_scores.values())
        agreement_signals.append(
            "bullish" if total_score > 0 else ("bearish" if total_score < 0 else "neutral")
        )
    dcf = quant.get("dcf_valuation") or {}
    if dcf.get("upside_pct") is not None:
        agreement_signals.append("bullish" if dcf["upside_pct"] > 0 else "bearish")
    if agreement_signals:
        rec_dir = "bullish" if rec == "BUY" else ("bearish" if rec == "SELL" else "neutral")
        matching = sum(1 for s in agreement_signals if s == rec_dir)
        agreement = matching / len(agreement_signals)
    else:
        agreement = 0.5

    # Source quality: reward having more validated data sources
    quality_score = 0.0
    if quant.get("dcf_valuation"):
        quality_score += 0.25
    if quant.get("peer_comparison") and (quant["peer_comparison"] or {}).get("rankings"):
        quality_score += 0.25
    if quant.get("stress_test"):
        quality_score += 0.15
    if quant.get("insider_signals"):
        quality_score += 0.15
    if quant.get("options_signals") and (
        quant["options_signals"] or {}
    ).get("flow_signal") != "no_data":
        quality_score += 0.20
    source_quality = min(quality_score, 1.0)

    confidence = 0.30 * freshness + 0.25 * agreement + 0.25 * source_quality + 0.20 * 0.5
    return round(max(0.1, min(1.0, confidence)), 2)


def _derive_rag_confidence(rag: dict) -> float:
    """Derive a confidence score from RAG data."""
    summary = rag.get("summary") or ""
    if _looks_like_error(summary):
        logger.warning("RAG summary looks like an error, assigning near-zero confidence")
        return 0.05

    explicit = rag.get("confidence_score")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return explicit

    score = 0.3
    if summary and len(summary) > 50:
        score += 0.2
    sources = rag.get("sources") or []
    if sources:
        score += min(len(sources) * 0.1, 0.3)
    return min(score, 1.0)


def _derive_market_confidence(market: dict) -> float:
    """Derive a confidence score from market context data."""
    narrative = market.get("narrative") or ""
    if _looks_like_error(narrative):
        logger.warning(
            "Market context narrative looks like an error, assigning near-zero confidence"
        )
        return 0.05

    explicit = market.get("confidence_score")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return explicit

    score = 0.3
    if narrative:
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

    # Completeness: ratio of non-empty fields across all agents
    total_fields = 0
    filled_fields = 0
    for _, agent_data in [
        ("quant", quant),
        ("rag", rag),
        ("market_context", market),
        ("analytics", analytics),
    ]:
        if isinstance(agent_data, dict):
            for k in agent_data:
                if k.startswith("_"):
                    continue
                total_fields += 1
                v = agent_data[k]
                if v is not None and (not isinstance(v, (list, dict)) or len(v) > 0):
                    filled_fields += 1
    completeness = filled_fields / total_fields if total_fields > 0 else 0.0

    # Consistency: penalize when contradicting signals are present (use agreement as proxy)
    consistency = agreement

    # Freshness: whether key time-sensitive data is present
    freshness_indicators = [
        bool(quant.get("technicals")),
        bool(quant.get("positioning")),
        bool(market.get("macro_regime")),
        bool(analytics.get("trend_analysis")),
    ]
    freshness = sum(freshness_indicators) / len(freshness_indicators)

    # Verification: ratio of agents with non-empty source data
    verified_agents = sum([
        bool(rag.get("sources")),
        bool(quant.get("fundamentals")),
        bool(market.get("narrative")),
        bool(analytics.get("statistical_summary")),
    ])
    verification = verified_agents / 4

    data_quality = (
        0.35 * completeness
        + 0.35 * consistency
        + 0.20 * freshness
        + 0.10 * verification
    )
    data_quality = max(0.0, min(1.0, data_quality))

    avg_agent_conf = (quant_conf + rag_conf + market_conf + analytics_conf) / 4
    meta_confidence = (
        0.35 * avg_agent_conf
        + 0.25 * agreement
        + 0.25 * consistency
        + 0.15 * verification
    )
    meta_confidence = max(0.0, min(1.0, meta_confidence))

    logger.info(
        "Confidence: meta=%.2f (agreement=%.2f, quality=%.2f,"
        " agents=[quant=%.2f, rag=%.2f, market=%.2f, analytics=%.2f])",
        meta_confidence,
        agreement,
        data_quality,
        quant_conf,
        rag_conf,
        market_conf,
        analytics_conf,
    )
    return ConfidenceBreakdown(
        agent_scores=AgentScores(
            quant=round(quant_conf, 2),
            rag=round(rag_conf, 2),
            market_context=round(market_conf, 2),
            analytics=round(analytics_conf, 2),
        ),
        agreement_score=round(agreement, 2),
        data_quality_score=round(data_quality, 2),
        meta_confidence=round(meta_confidence, 2),
    ).model_dump()
