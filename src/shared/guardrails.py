"""Shared input guardrails for investment topic filtering."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_NON_INVESTMENT_RE = re.compile(
    r"\b(weather|recipe|sports score|movie|song|joke|cook(?:ing)?|weather forecast|"
    r"horoscope|gaming|video game|celebrity|fashion|travel destination)\b",
    re.IGNORECASE,
)


def is_off_topic(text: str) -> bool:
    """Return True if `text` is clearly unrelated to investment/finance."""
    result = bool(_NON_INVESTMENT_RE.search(text))
    if result:
        logger.info("Off-topic query blocked: %.80s", text)
    return result


# Re-export so callers can import from one place
from shared.ticker_utils import extract_ticker  # noqa: E402, F401
