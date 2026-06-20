"""Source verification tool that checks citations and data provenance in agent outputs."""

import logging

from shared.agent_models import SourceVerification

logger = logging.getLogger(__name__)


def _safe_get(d: dict, *keys, default=None):
    """Safely traverse a nested dict returning the value at `keys` or `default` if any key is missing."""
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, {})
        else:
            return default
    return d if d is not None else default


def verify_sources(agent_outputs: dict) -> list[dict]:
    """Check factual claims across agent outputs: DCF upside consistency, RAG sources presence, market
    signal enum validity, and analytics forecast dates / chart data. Returns a list of SourceVerification
    dicts with claim-level pass/fail detail per agent."""
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
            verifications.append(SourceVerification(
                agent_name="Quant Analysis Agent",
                claims_checked=1,
                claims_verified=0,
                unverified_claims=[
                    f"DCF upside_pct ({dcf_upside:.1f}%) != calculated ({calculated_upside:.1f}%)"
                ],
            ).model_dump())

    # Sharpe ratio and VaR(95%) range checks are enforced by
    # QuantRiskMetrics Pydantic Field constraints.

    # RAG confidence_score [0,1] is enforced by RAGAgentOutput Pydantic model.
    rag_sources = rag.get("sources", [])
    rag_summary = rag.get("summary", "")
    if rag_summary and not rag_sources:
        verifications.append(SourceVerification(
            agent_name="Financial RAG Agent",
            claims_checked=1,
            claims_verified=0,
            unverified_claims=["RAG summary present but no sources listed"],
        ).model_dump())

    # Market confidence_score [0,1] is enforced by MarketContextOutput Pydantic model.
    market_signal = market.get("overall_signal")
    if market_signal and market_signal.lower() not in ("bullish", "bearish", "neutral"):
        verifications.append(
            {
                "agent_name": "Market Context Agent",
                "claims_checked": 1,
                "claims_verified": 0,
                "verification_rate": 0.0,
                "unverified_claims": [f"Market signal '{market_signal}' not one of bullish/bearish/neutral"],
            }
        )

    # analytics_confidence [0,1] is enforced by AnalyticsAgentOutput Pydantic model.
    forecast = analytics.get("forecast") or {}
    charts = analytics.get("charts", [])
    claims_checked = 0
    claims_verified = 0
    unverified = []
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
        verifications.append(SourceVerification(
            agent_name="Analytics Agent",
            claims_checked=claims_checked,
            claims_verified=claims_verified,
            verification_rate=round(rate, 2),
            unverified_claims=unverified,
        ).model_dump())

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
