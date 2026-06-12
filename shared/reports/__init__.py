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
from shared.reports.pptx_renderer import generate_pptx as _legacy_generate_pptx


def generate_pptx(
    brief_data: dict,
    ticker: str,
    recommendation: str,
    confidence: float,
    analysis_date: str,
) -> BytesIO:
    """Generate PPTX: try Playwright sync wrapper first, fall back to legacy."""
    try:
        from shared.reports.playwright_export import html_to_pptx_sync

        html_str = generate_html(brief_data, ticker, recommendation, confidence, analysis_date)
        return html_to_pptx_sync(html_str)
    except Exception:
        logger.debug("Playwright PPTX sync failed, using legacy renderer", exc_info=True)
        return _legacy_generate_pptx(brief_data, ticker, recommendation, confidence, analysis_date)


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
        logger.debug("Playwright PPTX async failed, using legacy renderer", exc_info=True)
        return _legacy_generate_pptx(brief_data, ticker, recommendation, confidence, analysis_date)


async def generate_pdf_async(
    brief_data: dict,
    ticker: str,
    recommendation: str,
    confidence: float,
    analysis_date: str,
) -> BytesIO:
    """Generate PDF via Playwright print mode. Falls back to HTML if Playwright fails."""
    try:
        from shared.reports.playwright_export import html_to_pdf

        html_str = generate_html(brief_data, ticker, recommendation, confidence, analysis_date)
        return await html_to_pdf(html_str)
    except Exception:
        logger.debug("Playwright PDF failed, returning HTML as fallback", exc_info=True)
        html_str = generate_html(brief_data, ticker, recommendation, confidence, analysis_date)
        return BytesIO(html_str.encode("utf-8"))


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
