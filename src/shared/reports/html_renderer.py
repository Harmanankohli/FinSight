# ruff: noqa: E402
"""Jinja2 HTML report generation."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from shared.reports.deck_model import DeckData
from shared.reports.extraction import _extract_deck_data


def _deck_to_template_context(deck: DeckData) -> dict:
    """Build Jinja2 template context from DeckData."""
    rec_colors = {"BUY": "var(--green)", "HOLD": "var(--blue)", "SELL": "var(--red)"}
    conf = deck.confidence / 100.0 if deck.confidence > 1.0 else deck.confidence
    confidence_pct = f"{conf:.0%}"

    scenario_cards = []
    if deck.scenarios.get("bull"):
        scenario_cards.append(("Bull Case", deck.scenarios["bull"], "var(--green-dark)"))
    if deck.scenarios.get("base"):
        scenario_cards.append(("Base Case", deck.scenarios["base"], "var(--blue)"))
    if deck.scenarios.get("dcf"):
        scenario_cards.append(("DCF Fair Value", deck.scenarios["dcf"], "var(--amber)"))
    if deck.scenarios.get("bear"):
        scenario_cards.append(("Bear Case", deck.scenarios["bear"], "var(--red)"))

    return {
        "deck": deck,
        "rec_colors": rec_colors,
        "confidence_pct": confidence_pct,
        "scenario_cards": scenario_cards,
    }


_jinja_env: "jinja2.Environment | None" = None  # noqa: F821


def _get_jinja_env() -> "jinja2.Environment":  # noqa: F821
    global _jinja_env
    if _jinja_env is None:
        from pathlib import Path

        import jinja2

        template_dir = Path(__file__).parent.parent / "templates"
        _jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(template_dir)),
            autoescape=True,
        )
    return _jinja_env


def generate_html(
    brief_data: dict,
    ticker: str,
    recommendation: str,
    confidence: float,
    analysis_date: str,
    company_info: dict | None = None,
) -> str:
    """Generate a standalone HTML investment deck. Returns HTML string."""
    logger.info("Generating HTML report for %s", ticker)
    deck = _extract_deck_data(
        brief_data, ticker, recommendation, confidence, analysis_date, company_info=company_info
    )
    ctx = _deck_to_template_context(deck)
    template = _get_jinja_env().get_template("investment_deck.html")
    result = template.render(**ctx)
    logger.info("HTML report generated for %s (%d bytes)", ticker, len(result))
    return result
