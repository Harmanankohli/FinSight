"""shared.reports — investment report generation package.

Public API:
    generate_pptx(brief_data, ticker, recommendation, confidence, analysis_date) -> BytesIO
    generate_docx(brief_data, ticker, recommendation, confidence, analysis_date) -> BytesIO
    generate_html(brief_data, ticker, recommendation, confidence, analysis_date) -> str
"""

from shared.reports.deck_model import DeckData, ExtractionCtx, ParsedTable, Section
from shared.reports.pptx_renderer import generate_pptx
from shared.reports.docx_renderer import generate_docx
from shared.reports.html_renderer import generate_html

__all__ = [
    "generate_pptx",
    "generate_docx",
    "generate_html",
    "DeckData",
    "ParsedTable",
    "Section",
]
