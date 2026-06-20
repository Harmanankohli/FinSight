"""Phase 5: PPTX Generator Integration Tests.

End-to-end verification of generate_pptx() with real data patterns,
edge cases, and unknown tickers. Mirrors the DOCX regression tests
structure but validates PPTX-specific output properties.
"""

import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("yfinance", reason="report regression tests require yfinance")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.reports import generate_pptx

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


def _count_slides(buf: BytesIO) -> int:
    """Count slides in a PPTX by parsing the zip structure."""
    import zipfile

    with zipfile.ZipFile(buf) as z:
        slide_files = [
            n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        ]
    return len(slide_files)


def test_pptx_generates_valid_output(tmp_path):
    """Realistic WMT data → PPTX valid, ≥6 slides, non-empty."""
    with patch("yfinance.Ticker", _make_yf_mock("Walmart Inc.", "Consumer Defensive", "NYQ")):
        buf = generate_pptx(
            {"response_text": WMT_RESPONSE_TEXT},
            "WMT",
            "HOLD",
            0.58,
            "2026-06-08",
        )
    assert isinstance(buf, BytesIO)
    data = buf.read()
    assert len(data) > 5000, f"PPTX too small: {len(data)} bytes"
    n_slides = _count_slides(BytesIO(data))
    assert n_slides >= 6, f"Expected ≥6 slides, got {n_slides}"
    out = tmp_path / "WMT_report.pptx"
    out.write_bytes(data)
    print(f"WMT PPTX written to {out} ({len(data)} bytes, {n_slides} slides)")


def test_pptx_with_empty_brief(tmp_path):
    """Empty brief_data → minimal deck, title + thesis + conclusion, no crash."""
    with patch("yfinance.Ticker", side_effect=Exception("network fail")):
        buf = generate_pptx({}, "XYZ", "UNKNOWN", 0.0, "2026-01-01")
    assert isinstance(buf, BytesIO)
    data = buf.read()
    assert len(data) > 2000
    n_slides = _count_slides(BytesIO(data))
    assert n_slides >= 3
    out = tmp_path / "XYZ_empty.pptx"
    out.write_bytes(data)
    print(f"XYZ (empty) PPTX written to {out} ({len(data)} bytes, {n_slides} slides)")


def test_pptx_with_unknown_ticker(tmp_path):
    """Unknown ticker → yfinance fallback, PPTX still valid."""
    text = "Analysis of Palantir Technologies (PLTR) shows strong growth."
    with patch("yfinance.Ticker", side_effect=Exception("network fail")):
        buf = generate_pptx(
            {"response_text": text},
            "PLTR",
            "BUY",
            0.70,
            "2026-06-08",
        )
    assert isinstance(buf, BytesIO)
    data = buf.read()
    assert len(data) > 2000
    out = tmp_path / "PLTR_report.pptx"
    out.write_bytes(data)
    print(f"PLTR PPTX written to {out} ({len(data)} bytes)")


def test_pptx_with_nonstandard_rec(tmp_path):
    """Non-standard recommendation → no crash, mapped to default color."""
    text = "Analysis of Test Corp (TST) shows potential."
    with patch("yfinance.Ticker", side_effect=Exception("network fail")):
        buf = generate_pptx(
            {"response_text": text},
            "TST",
            "STRONG BUY",
            0.80,
            "2026-06-08",
        )
    assert isinstance(buf, BytesIO)
    data = buf.read()
    assert len(data) > 2000
    out = tmp_path / "TST_nonstd_rec.pptx"
    out.write_bytes(data)
    print(f"TST (non-standard rec) PPTX written to {out} ({len(data)} bytes)")


def test_pptx_with_unicode(tmp_path):
    """Unicode characters in company name → no encoding errors."""
    text = "Analysis of São Paulo Alimentos (SPA) shows growth."
    with patch(
        "yfinance.Ticker", _make_yf_mock("São Paulo Alimentos S.A.", "Consumer Defensive", "NYQ")
    ):
        buf = generate_pptx(
            {"response_text": text},
            "SPA",
            "BUY",
            0.65,
            "2026-06-08",
        )
    assert isinstance(buf, BytesIO)
    data = buf.read()
    assert len(data) > 2000
    out = tmp_path / "SPA_unicode.pptx"
    out.write_bytes(data)
    print(f"SPA (unicode) PPTX written to {out} ({len(data)} bytes)")


def test_pptx_with_markdown_tables(tmp_path):
    """Markdown tables in response_text → parsed, PPTX renders without empty slides."""
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
        buf = generate_pptx(
            {"response_text": text},
            "WMT",
            "HOLD",
            0.58,
            "2026-06-08",
        )
    assert isinstance(buf, BytesIO)
    data = buf.read()
    assert len(data) > 2000
    out = tmp_path / "WMT_tables.pptx"
    out.write_bytes(data)
    print(f"WMT (tables) PPTX written to {out} ({len(data)} bytes)")


def test_pptx_very_long_summary(tmp_path):
    """Very long executive summary → truncated, no text overflow in PPTX."""
    long_text = "## Investment Thesis\n\n" + "A" * 3000 + "\n\n## Key Risks\n- Risk one"
    with patch("yfinance.Ticker", side_effect=Exception("network fail")):
        buf = generate_pptx(
            {"response_text": long_text},
            "LONG",
            "HOLD",
            0.50,
            "2026-06-08",
        )
    assert isinstance(buf, BytesIO)
    data = buf.read()
    assert len(data) > 2000
    out = tmp_path / "LONG_summary.pptx"
    out.write_bytes(data)
    print(f"LONG (summary) PPTX written to {out} ({len(data)} bytes)")


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_pptx_generates_valid_output(tmp)
        test_pptx_with_empty_brief(tmp)
        test_pptx_with_unknown_ticker(tmp)
        test_pptx_with_nonstandard_rec(tmp)
        test_pptx_with_unicode(tmp)
        test_pptx_with_markdown_tables(tmp)
        test_pptx_very_long_summary(tmp)
    print("\nAll PPTX regression tests passed.")
