# ruff: noqa: E402
"""shared.reports — investment report generation package.

Public API:
    generate_pptx(brief_data, ticker, recommendation, confidence, analysis_date) -> BytesIO
    generate_pptx_async(brief_data, ticker, recommendation, confidence, analysis_date) -> BytesIO
    generate_docx(brief_data, ticker, recommendation, confidence, analysis_date) -> BytesIO
    generate_html(brief_data, ticker, recommendation, confidence, analysis_date) -> str
    generate_pdf_async(brief_data, ticker, recommendation, confidence, analysis_date) -> BytesIO
"""

from __future__ import annotations

import logging
from io import BytesIO

logger = logging.getLogger(__name__)

from shared.reports.deck_model import DeckData, ExtractionCtx, ParsedTable, Section
from shared.reports.docx_renderer import generate_docx
from shared.reports.html_renderer import generate_html
from shared.reports.pptx_renderer import generate_pptx


async def generate_pptx_async(
    brief_data: dict,
    ticker: str,
    recommendation: str,
    confidence: float,
    analysis_date: str,
) -> BytesIO:
    """Generate PPTX via Playwright screenshots, falling back to legacy renderer."""
    try:
        from shared.reports.playwright_export import html_to_pptx

        html_str = generate_html(brief_data, ticker, recommendation, confidence, analysis_date)
        return await html_to_pptx(html_str)
    except Exception:
        logger.debug("Playwright PPTX failed, using legacy renderer", exc_info=True)
        return generate_pptx(brief_data, ticker, recommendation, confidence, analysis_date)


async def generate_pdf_async(
    brief_data: dict,
    ticker: str,
    recommendation: str,
    confidence: float,
    analysis_date: str,
) -> BytesIO:
    """Generate PDF via Playwright print mode."""
    from shared.reports.playwright_export import html_to_pdf

    html_str = generate_html(brief_data, ticker, recommendation, confidence, analysis_date)
    return await html_to_pdf(html_str)


__all__ = [
    "generate_pptx",
    "generate_pptx_async",
    "generate_docx",
    "generate_html",
    "generate_pdf_async",
    "DeckData",
    "ExtractionCtx",
    "ParsedTable",
    "Section",
]
