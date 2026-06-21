"""Bank-specific valuation using P/TBV and P/E multiples.

Provides an alternative to DCF for financial institutions.
"""
import logging

from shared.agent_models import DCFValuation

logger = logging.getLogger(__name__)


def run_bank_valuation(
    ticker: str,
    sector: str,
    current_price: float,
    price_to_book: float | None,
    monte_carlo: dict | None = None,
) -> dict:
    """P/B-based valuation for financial sector stocks where DCF is unreliable."""
    if not price_to_book or price_to_book <= 0 or current_price <= 0:
        return {
            "dcf_valuation": None,
            "dcf_error": (
                f"Valuation skipped: {sector} sector"
                " — DCF not applicable. P/B ratio unavailable."
            ),
            "monte_carlo": monte_carlo,
        }

    book_value_ps = current_price / price_to_book
    fair_pb = 1.2
    pb_fair_value = book_value_ps * fair_pb
    pb_upside = (pb_fair_value - current_price) / current_price

    logger.info(
        "P/B valuation for %s (sector=%s): P/B=%.2f, fair_P/B=%.1f, intrinsic=$%.2f, upside=%.1f%%",
        ticker, sector, price_to_book, fair_pb, pb_fair_value, pb_upside * 100,
    )

    return {
        "dcf_valuation": DCFValuation(
            intrinsic_value=round(pb_fair_value, 2),
            current_price=round(current_price, 2),
            upside_pct=round(pb_upside * 100, 1),
            method="pb",
            pb_ratio_used=round(price_to_book, 2),
            fair_pb_multiple=fair_pb,
        ).model_dump(),
        "monte_carlo": monte_carlo,
    }
