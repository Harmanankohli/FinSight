import logging

logger = logging.getLogger(__name__)


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

    # ── DCF valuation ──
    dcf = quant.get("dcf_valuation") or {}
    upside = dcf.get("upside_pct")
    if upside is not None:
        if upside > 20:
            supporting.append(f"DCF upside of {upside:.1f}%")
        elif upside < -10:
            contradicting.append(f"DCF downside of {upside:.1f}%")

    # ── Technical trend ──
    trend = _safe_get(analytics, "trend_analysis", "trend_direction", default="neutral")
    if trend == "bullish":
        supporting.append("Bullish technical trend")
    elif trend == "bearish":
        contradicting.append("Bearish technical trend")

    # ── MA crossover ──
    ma_cross = _safe_get(analytics, "trend_analysis", "ma_crossover_signal", default=None)
    if ma_cross == "golden_cross":
        supporting.append("Golden cross detected")
    elif ma_cross == "death_cross":
        contradicting.append("Death cross detected")

    # ── Market context signal ──
    market_signal = market.get("overall_signal", "neutral")
    if market_signal == "bullish":
        supporting.append("Bullish macro context")
    elif market_signal == "bearish":
        contradicting.append("Bearish macro context")

    # ── RAG filings sentiment ──
    rag_summary = rag.get("summary", "")
    if rag_summary:
        positive_words = [
            "growth",
            "profit",
            "positive",
            "strong",
            "beat",
            "exceed",
            "improvement",
            "record",
        ]
        negative_words = [
            "decline",
            "loss",
            "negative",
            "weak",
            "miss",
            "risk",
            "warning",
            "deteriorat",
        ]
        summary_lower = rag_summary.lower()
        pos_matched = [w for w in positive_words if w in summary_lower]
        neg_matched = [w for w in negative_words if w in summary_lower]
        if len(pos_matched) > len(neg_matched):
            supporting.append(
                f"Positive RAG filing signals ({', '.join(pos_matched)})"
            )
        if len(neg_matched) > len(pos_matched):
            contradicting.append(
                f"Negative RAG filing signals ({', '.join(neg_matched)})"
            )

    # ── Quant signal ──
    quant_signal = _safe_get(quant, "metrics", "quant_signal", default="neutral")
    if quant_signal == "bullish":
        supporting.append("Bullish quant signal")
    elif quant_signal == "bearish":
        contradicting.append("Bearish quant signal")

    # ── Anomaly severity ──
    anomaly_severity = _safe_get(analytics, "anomalies", "severity", default="none")
    if anomaly_severity in ("medium", "high"):
        count = int(_safe_get(analytics, "anomalies", "anomaly_count", default=0))
        contradicting.append(
            f"Analytics anomaly severity {anomaly_severity} ({count} anomalies detected)"
        )

    # ── Monte Carlo probability ──
    mc = quant.get("monte_carlo") or {}
    prob_profit = mc.get("prob_profit")
    if prob_profit is not None:
        if prob_profit > 0.65:
            supporting.append(f"Monte Carlo {prob_profit:.0%} probability of profit")
        elif prob_profit < 0.40:
            contradicting.append(f"Monte Carlo only {prob_profit:.0%} probability of profit")

    # ── Fundamentals ──
    fundamentals = quant.get("fundamentals") or {}
    roe = fundamentals.get("roe")
    if roe is not None:
        if roe > 0.20:
            supporting.append(
                f"Strong ROE of {roe * 100:.1f}%" if roe < 5 else f"Strong ROE of {roe:.1f}%"
            )
        elif roe < 0:
            contradicting.append(f"Negative ROE of {roe * 100:.1f}%")

    debt_eq = fundamentals.get("debt_to_equity")
    if debt_eq is not None and debt_eq > 2.0:
        contradicting.append(f"High debt/equity ratio of {debt_eq:.1f}")

    rev_growth = fundamentals.get("revenue_growth")
    if rev_growth is not None:
        if rev_growth > 0.10:
            supporting.append(
                f"Revenue growth of {rev_growth * 100:.1f}%"
                if rev_growth < 5
                else f"Revenue growth of {rev_growth:.1f}%"
            )
        elif rev_growth < -0.05:
            contradicting.append(f"Revenue decline of {rev_growth * 100:.1f}%")

    # ── Technicals: RSI ──
    technicals = quant.get("technicals") or {}
    rsi = technicals.get("rsi_14") or technicals.get("rsi")
    if rsi is not None:
        if rsi > 70:
            contradicting.append(f"RSI overbought at {rsi:.0f}")
        elif rsi < 30:
            supporting.append(f"RSI oversold at {rsi:.0f} (potential bounce)")

    # ── Golden cross from technicals ──
    golden = technicals.get("golden_cross")
    if golden is True:
        supporting.append("Golden cross (50d > 200d MA)")

    # ── Market tailwinds/headwinds ──
    tailwinds = market.get("key_tailwinds") or []
    for tw in tailwinds[:3]:
        supporting.append(f"Macro tailwind: {tw}")
    headwinds = market.get("key_headwinds") or []
    for hw in headwinds[:3]:
        contradicting.append(f"Macro headwind: {hw}")

    # ── Analytics confidence ──
    analytics_conf = analytics.get("analytics_confidence")
    if analytics_conf is not None and analytics_conf > 0:
        if analytics_conf > 0.7:
            supporting.append(f"High analytics confidence ({analytics_conf:.0%})")
        elif analytics_conf < 0.3:
            contradicting.append(f"Low analytics confidence ({analytics_conf:.0%})")

    # ── Sharpe ratio ──
    sharpe = _safe_get(quant, "metrics", "sharpe_ratio", default=None)
    if isinstance(sharpe, (int, float)) and sharpe != 0:
        if sharpe > 1.0:
            supporting.append(f"Strong risk-adjusted return (Sharpe {sharpe:.2f})")
        elif sharpe < 0:
            contradicting.append(f"Negative Sharpe ratio ({sharpe:.2f})")

    # ── Determine evidence support ──
    if recommendation == "BUY":
        evidence_supports = len(supporting) > len(contradicting)
    elif recommendation == "SELL":
        evidence_supports = len(contradicting) > len(supporting)
    else:
        evidence_supports = True

    total_evidence = len(supporting) + len(contradicting)
    if total_evidence >= 8:
        strength = "strong"
    elif total_evidence >= 4:
        strength = "moderate"
    else:
        strength = "weak"

    logger.info(
        "Validation: %s (evidence supports=%s, strength=%s, supporting=%d, contradicting=%d)",
        recommendation,
        evidence_supports,
        strength,
        len(supporting),
        len(contradicting),
    )
    return {
        "recommendation": recommendation,
        "evidence_supports": evidence_supports,
        "supporting_evidence": supporting,
        "contradicting_evidence": contradicting,
        "evidence_strength": strength,
    }
