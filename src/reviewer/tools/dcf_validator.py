"""DCF valuation validation tool that checks for sanity and flags unrealistic results."""

import logging

logger = logging.getLogger(__name__)


def validate_dcf(quant: dict) -> dict:
    """Validate DCF output for sanity and flag unrealistic results."""
    dcf = quant.get("dcf_valuation") or {}
    if not dcf:
        return {"checks": [], "overall_status": "skipped"}

    intrinsic = dcf.get("intrinsic_value")
    current_price = dcf.get("current_price")
    checks = []

    if intrinsic is not None and current_price and current_price > 0:
        ratio = intrinsic / current_price
        if ratio < 0.30:
            checks.append({
                "check": "low_valuation",
                "status": "warning",
                "message": (
                    f"DCF value ({intrinsic:.2f}) is below 30%"
                    f" of market price ({current_price:.2f})"
                ),
            })
        elif ratio > 3.0:
            checks.append({
                "check": "high_valuation",
                "status": "warning",
                "message": (
                    f"DCF value ({intrinsic:.2f}) is above 300%"
                    f" of market price ({current_price:.2f})"
                ),
            })
        else:
            checks.append({
                "check": "valuation_ratio",
                "status": "ok",
                "message": f"DCF/market ratio: {ratio:.2f}x",
            })

    wacc = dcf.get("wacc")
    if wacc is not None:
        if wacc < 0.05 or wacc > 0.20:
            checks.append({
                "check": "wacc_range",
                "status": "warning",
                "message": f"WACC {wacc:.1%} is outside typical range (5%-20%)",
            })
        else:
            checks.append({
                "check": "wacc_range",
                "status": "ok",
                "message": f"WACC {wacc:.1%} is within normal range",
            })

    growth = dcf.get("growth_rate")
    if growth is not None:
        if growth > 0.25:
            checks.append({
                "check": "growth_rate",
                "status": "warning",
                "message": f"Growth rate {growth:.1%} exceeds 25% cap",
            })
        else:
            checks.append({
                "check": "growth_rate",
                "status": "ok",
                "message": f"Growth rate {growth:.1%}",
            })

    fcf = dcf.get("fcf_used")
    if fcf is not None and fcf <= 0:
        checks.append({
            "check": "fcf_positive",
            "status": "warning",
            "message": f"FCF used is non-positive: {fcf}",
        })

    fcf_age = dcf.get("fcf_age_days") or 0
    if fcf_age > 365:
        checks.append({
            "check": "stale_financials",
            "status": "warning",
            "message": (
                f"Financial data is {fcf_age} days old"
                " — data may be significantly outdated"
            ),
        })
    elif fcf_age > 180:
        checks.append({
            "check": "stale_financials",
            "status": "warning",
            "message": (
                f"Financial data is {fcf_age} days old"
                " — consider verifying with latest filings"
            ),
        })

    warnings = dcf.get("valuation_warnings") or []
    for w in warnings:
        checks.append({
            "check": "valuation_warning",
            "status": "warning",
            "message": w,
        })

    n_warnings = sum(1 for c in checks if c["status"] == "warning")
    overall = "pass" if n_warnings == 0 else ("warn" if n_warnings <= 2 else "fail")

    logger.info(
        "DCF validation: %s (%d checks, %d warnings)",
        overall, len(checks), n_warnings,
    )
    return {"checks": checks, "overall_status": overall}
