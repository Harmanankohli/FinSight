"""Phase 4: DOCX Generator Compatibility Verification.

The DOCX generator (generate_docx) reuses _extract_deck_data() via
_extract_docx_content(). Phase 1 extraction improvements apply automatically.
No DOCX code changes needed — just verify it still produces valid output
with populated sections after the Phase 1 refactor.
"""

import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("yfinance", reason="report regression tests require yfinance")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.reports import generate_docx

# ── Fixtures ────────────────────────────────────────────────────────────────────

WMT_RESPONSE_TEXT = (
    "## Investment Recommendation: HOLD\n\n"
    "Walmart Inc. (WMT) shows strong revenue growth (+7.3% YoY), "
    "ROE of 24.1%, and operating margin of 4.2%. "
    "P/E Ratio: 41.9x. Beta: 0.52. Dividend yield: 1.3%. "
    "DCF fair value: $73.77. Analyst price target: $145.25.\n\n"
    "## Financial Performance\n"
    "Revenue grew to $177.8B driven by e-commerce growth. "
    "Operating income improved by 5.6%.\n\n"
    "## Valuation\n"
    "P/E Ratio: 41.9. Analyst price target: $145.25. "
    "DCF fair value: $73.77.\n\n"
    "## Growth Opportunities\n"
    "- E-commerce expansion and omnichannel capabilities\n"
    "- International market penetration\n"
    "- Advertising business growth (Walmart Connect)\n\n"
    "## Key Risks\n"
    "- Premium valuation multiples\n"
    "- MACD bearish momentum signal\n"
    "- Competitive pressure from Costco (COST) and Amazon (AMZN)\n"
    "- Margin compression from inflation\n"
)


def _make_yf_mock(long_name, sector, exchange):
    mock = MagicMock()
    mock.return_value.info = {"longName": long_name, "sector": sector, "exchange": exchange}
    return mock


# ── Tests ───────────────────────────────────────────────────────────────────────


def test_docx_generates_valid_output(tmp_path):
    """Realistic WMT data → DOCX generates without error, non-empty."""
    with patch("yfinance.Ticker", _make_yf_mock("Walmart Inc.", "Consumer Defensive", "NYQ")):
        buf = generate_docx(
            {"response_text": WMT_RESPONSE_TEXT},
            "WMT",
            "HOLD",
            0.58,
            "2026-06-08",
        )
    assert isinstance(buf, BytesIO)
    data = buf.read()
    assert len(data) > 2000, f"DOCX too small: {len(data)} bytes"
    # Write for visual inspection
    out = tmp_path / "WMT_report.docx"
    out.write_bytes(data)
    print(f"WMT DOCX written to {out} ({len(data)} bytes)")


def test_docx_with_empty_brief(tmp_path):
    """Empty brief_data → still generates, minimal content, no crash."""
    with patch("yfinance.Ticker", side_effect=Exception("network fail")):
        buf = generate_docx({}, "XYZ", "UNKNOWN", 0.0, "2026-01-01")
    assert isinstance(buf, BytesIO)
    data = buf.read()
    assert len(data) > 1000
    out = tmp_path / "XYZ_empty.docx"
    out.write_bytes(data)
    print(f"XYZ (empty) DOCX written to {out} ({len(data)} bytes)")


def test_docx_with_unknown_ticker(tmp_path):
    """Unknown ticker → yfinance fallback, DOCX still produces valid output."""
    text = "Analysis of Palantir Technologies (PLTR) shows strong growth."
    with patch("yfinance.Ticker", side_effect=Exception("network fail")):
        buf = generate_docx(
            {"response_text": text},
            "PLTR",
            "BUY",
            0.70,
            "2026-06-08",
        )
    assert isinstance(buf, BytesIO)
    data = buf.read()
    assert len(data) > 1000
    out = tmp_path / "PLTR_report.docx"
    out.write_bytes(data)
    print(f"PLTR DOCX written to {out} ({len(data)} bytes)")


def test_docx_with_unicode(tmp_path):
    """Unicode characters in company name → no encoding errors."""
    text = "Analysis of São Paulo Alimentos (SPA) shows growth."
    with patch(
        "yfinance.Ticker", _make_yf_mock("São Paulo Alimentos S.A.", "Consumer Defensive", "NYQ")
    ):
        buf = generate_docx(
            {"response_text": text},
            "SPA",
            "BUY",
            0.65,
            "2026-06-08",
        )
    assert isinstance(buf, BytesIO)
    data = buf.read()
    assert len(data) > 1000
    out = tmp_path / "SPA_unicode.docx"
    out.write_bytes(data)
    print(f"SPA (unicode) DOCX written to {out} ({len(data)} bytes)")


def test_docx_with_markdown_tables(tmp_path):
    """Markdown tables in response_text → parsed into structured data, DOCX renders."""
    text = (
        "## Financial Summary\n\n"
        "| Metric | Current | YoY Change |\n"
        "|--------|---------|------------|\n"
        "| Revenue | $177.8B | +7.3% |\n"
        "| Operating Income | $7.5B | +5.6% |\n"
        "| Net Income | $5.1B | +4.2% |\n"
        "\nWalmart shows strong performance across all metrics."
    )
    with patch("yfinance.Ticker", _make_yf_mock("Walmart Inc.", "Consumer Defensive", "NYQ")):
        buf = generate_docx(
            {"response_text": text},
            "WMT",
            "HOLD",
            0.58,
            "2026-06-08",
        )
    assert isinstance(buf, BytesIO)
    data = buf.read()
    assert len(data) > 1000
    out = tmp_path / "WMT_tables.docx"
    out.write_bytes(data)
    print(f"WMT (tables) DOCX written to {out} ({len(data)} bytes)")


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_docx_generates_valid_output(tmp)
        test_docx_with_empty_brief(tmp)
        test_docx_with_unknown_ticker(tmp)
        test_docx_with_unicode(tmp)
        test_docx_with_markdown_tables(tmp)
    print("\nAll DOCX regression tests passed.")
