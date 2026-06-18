import logging

logger = logging.getLogger(__name__)


def _safe_get(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, {})
        else:
            return default
    return d if d is not None else default


def verify_sources(agent_outputs: dict) -> list[dict]:
    verifications = []

    quant = agent_outputs.get("quant", {})
    rag = agent_outputs.get("rag", {})
    market = agent_outputs.get("market_context", {})
    analytics = agent_outputs.get("analytics", {})

    dcf = quant.get("dcf_valuation") or {}
    dcf_intrinsic = dcf.get("intrinsic_value") or dcf.get("fair_value")
    dcf_current = dcf.get("current_price")
    dcf_upside = dcf.get("upside_pct")
    if dcf_intrinsic and dcf_current and dcf_upside is not None:
        calculated_upside = (dcf_intrinsic - dcf_current) / dcf_current * 100
        if abs(calculated_upside - dcf_upside) > 1.0:
            verifications.append(
                {
                    "agent_name": "Quant Analysis Agent",
                    "claims_checked": 1,
                    "claims_verified": 0,
                    "verification_rate": 0.0,
                    "unverified_claims": [
                        f"DCF upside_pct ({dcf_upside:.1f}%) != calculated ({calculated_upside:.1f}%)"
                    ],
                }
            )

    metrics = quant.get("metrics") or {}
    sharpe = metrics.get("sharpe_ratio")
    var_95 = metrics.get("var_95_daily")
    claims_checked = 0
    claims_verified = 0
    unverified = []
    if sharpe is not None:
        claims_checked += 1
        if -5 <= sharpe <= 5:
            claims_verified += 1
        else:
            unverified.append(f"Sharpe ratio {sharpe:.2f} out of plausible range [-5, 5]")
    if var_95 is not None:
        claims_checked += 1
        if -1 <= var_95 <= 0:
            claims_verified += 1
        else:
            unverified.append(f"VaR(95%) {var_95:.4f} out of range [-1, 0]")
    if claims_checked > 0:
        rate = claims_verified / claims_checked if claims_checked > 0 else 0.0
        verifications.append(
            {
                "agent_name": "Quant Analysis Agent",
                "claims_checked": claims_checked,
                "claims_verified": claims_verified,
                "verification_rate": round(rate, 2),
                "unverified_claims": unverified,
            }
        )

    rag_confidence = rag.get("confidence_score")
    rag_sources = rag.get("sources", [])
    rag_summary = rag.get("summary", "")
    claims_checked = 0
    claims_verified = 0
    unverified = []
    if rag_confidence is not None:
        claims_checked += 1
        if 0 <= rag_confidence <= 1:
            claims_verified += 1
        else:
            unverified.append(f"RAG confidence {rag_confidence} not in [0, 1]")
    if rag_summary:
        claims_checked += 1
        if rag_sources:
            claims_verified += 1
        else:
            unverified.append("RAG summary present but no sources listed")
    if claims_checked > 0:
        rate = claims_verified / claims_checked if claims_checked > 0 else 0.0
        verifications.append(
            {
                "agent_name": "Financial RAG Agent",
                "claims_checked": claims_checked,
                "claims_verified": claims_verified,
                "verification_rate": round(rate, 2),
                "unverified_claims": unverified,
            }
        )

    market_confidence = market.get("confidence_score")
    market_signal = market.get("overall_signal")
    claims_checked = 0
    claims_verified = 0
    unverified = []
    if market_confidence is not None:
        claims_checked += 1
        if 0 <= market_confidence <= 1:
            claims_verified += 1
        else:
            unverified.append(f"Market confidence {market_confidence} not in [0, 1]")
    if market_signal:
        claims_checked += 1
        if market_signal.lower() in ("bullish", "bearish", "neutral"):
            claims_verified += 1
        else:
            unverified.append(f"Market signal '{market_signal}' not one of bullish/bearish/neutral")
    if claims_checked > 0:
        rate = claims_verified / claims_checked if claims_checked > 0 else 0.0
        verifications.append(
            {
                "agent_name": "Market Context Agent",
                "claims_checked": claims_checked,
                "claims_verified": claims_verified,
                "verification_rate": round(rate, 2),
                "unverified_claims": unverified,
            }
        )

    analytics_confidence = analytics.get("analytics_confidence")
    forecast = analytics.get("forecast") or {}
    charts = analytics.get("charts", [])
    claims_checked = 0
    claims_verified = 0
    unverified = []
    if analytics_confidence is not None:
        claims_checked += 1
        if 0 <= analytics_confidence <= 1:
            claims_verified += 1
        else:
            unverified.append(f"Analytics confidence {analytics_confidence} not in [0, 1]")
    forecast_dates = forecast.get("forecast_dates", [])
    if forecast_dates:
        claims_checked += 1
        from datetime import datetime

        if all(d >= datetime.now().strftime("%Y-%m-%d") for d in forecast_dates):
            claims_verified += 1
        else:
            unverified.append("Some forecast dates are in the past")
    if charts:
        claims_checked += 1
        if all(c.get("datasets") for c in charts):
            claims_verified += 1
        else:
            unverified.append("One or more charts have empty datasets")
    if claims_checked > 0:
        rate = claims_verified / claims_checked if claims_checked > 0 else 0.0
        verifications.append(
            {
                "agent_name": "Analytics Agent",
                "claims_checked": claims_checked,
                "claims_verified": claims_verified,
                "verification_rate": round(rate, 2),
                "unverified_claims": unverified,
            }
        )

    for v in verifications:
        if v.get("unverified_claims"):
            logger.warning(
                "Verification for %s: %d/%d passed — unverified: %s",
                v["agent_name"],
                v["claims_verified"],
                v["claims_checked"],
                v["unverified_claims"],
            )
        else:
            logger.info(
                "Verification for %s: %d/%d passed",
                v["agent_name"],
                v["claims_verified"],
                v["claims_checked"],
            )
    if not verifications:
        logger.info("Verification: no claims checked for any agent")
    return verifications
