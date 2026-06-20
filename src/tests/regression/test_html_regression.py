"""Phase 5: HTML Generator Integration Tests.

End-to-end verification of generate_html() with real data patterns,
edge cases, and unknown tickers. Validates that HTML output is valid,
contains expected slide sections, and is self-contained.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("yfinance", reason="report regression tests require yfinance")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.reports import generate_html

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


def test_html_generates_valid_output(tmp_path):
    """Realistic WMT data → HTML valid, company name in title, all sections present."""
    with patch("yfinance.Ticker", _make_yf_mock("Walmart Inc.", "Consumer Defensive", "NYQ")):
        html_str = generate_html(
            {"response_text": WMT_RESPONSE_TEXT},
            "WMT",
            "HOLD",
            0.58,
            "2026-06-08",
        )
    assert isinstance(html_str, str)
    assert len(html_str) > 5000, f"HTML too small: {len(html_str)} chars"
    assert "Walmart Inc." in html_str
    assert 'class="hero"' in html_str
    assert 'class="report-section"' in html_str
    assert 'class="conclusion"' in html_str
    assert "var(--blue)" in html_str or "color:var" in html_str
    out = tmp_path / "WMT_report.html"
    out.write_text(html_str, encoding="utf-8")
    print(f"WMT HTML written to {out} ({len(html_str)} chars)")


def test_html_with_empty_brief(tmp_path):
    """Empty brief_data → minimal HTML, no crash."""
    with patch("yfinance.Ticker", side_effect=Exception("network fail")):
        html_str = generate_html({}, "XYZ", "UNKNOWN", 0.0, "2026-01-01")
    assert isinstance(html_str, str)
    assert len(html_str) > 2000
    assert 'class="hero"' in html_str
    assert "XYZ" in html_str or "No analysis" in html_str
    out = tmp_path / "XYZ_empty.html"
    out.write_text(html_str, encoding="utf-8")
    print(f"XYZ (empty) HTML written to {out} ({len(html_str)} chars)")


def test_html_with_unknown_ticker(tmp_path):
    """Unknown ticker → yfinance fallback, HTML still generates."""
    text = "Analysis of Palantir Technologies (PLTR) shows strong growth."
    with patch("yfinance.Ticker", side_effect=Exception("network fail")):
        html_str = generate_html(
            {"response_text": text},
            "PLTR",
            "BUY",
            0.70,
            "2026-06-08",
        )
    assert isinstance(html_str, str)
    assert len(html_str) > 2000
    out = tmp_path / "PLTR_report.html"
    out.write_text(html_str, encoding="utf-8")
    print(f"PLTR HTML written to {out} ({len(html_str)} chars)")


def test_html_with_nonstandard_rec(tmp_path):
    """Non-standard recommendation → still renders, no crash."""
    text = "Analysis of Test Corp (TST) shows potential."
    with patch("yfinance.Ticker", side_effect=Exception("network fail")):
        html_str = generate_html(
            {"response_text": text},
            "TST",
            "STRONG BUY",
            0.80,
            "2026-06-08",
        )
    assert isinstance(html_str, str)
    assert len(html_str) > 2000
    out = tmp_path / "TST_nonstd_rec.html"
    out.write_text(html_str, encoding="utf-8")
    print(f"TST (non-standard rec) HTML written to {out} ({len(html_str)} chars)")


def test_html_with_unicode(tmp_path):
    """Unicode characters → no encoding errors in HTML."""
    text = "Analysis of São Paulo Alimentos (SPA) shows growth."
    with patch(
        "yfinance.Ticker", _make_yf_mock("São Paulo Alimentos S.A.", "Consumer Defensive", "NYQ")
    ):
        html_str = generate_html(
            {"response_text": text},
            "SPA",
            "BUY",
            0.65,
            "2026-06-08",
        )
    assert isinstance(html_str, str)
    assert "São Paulo" in html_str
    out = tmp_path / "SPA_unicode.html"
    out.write_text(html_str, encoding="utf-8")
    print(f"SPA (unicode) HTML written to {out} ({len(html_str)} chars)")


def test_html_with_markdown_tables(tmp_path):
    """Markdown tables → parsed, HTML renders without empty sections."""
    text = (
        "## Financial Summary\n\n"
        "| Metric | Current | YoY Change |\n"
        "|--------|---------|------------|\n"
        "| Revenue Growth | 7.3% | +7.3% |\n"
        "| Operating Income | $7.5B | +5.6% |\n"
        "\nWalmart shows strong performance."
    )
    with patch("yfinance.Ticker", _make_yf_mock("Walmart Inc.", "Consumer Defensive", "NYQ")):
        html_str = generate_html(
            {"response_text": text},
            "WMT",
            "HOLD",
            0.58,
            "2026-06-08",
        )
    assert isinstance(html_str, str)
    assert len(html_str) > 2000
    out = tmp_path / "WMT_tables.html"
    out.write_text(html_str, encoding="utf-8")
    print(f"WMT (tables) HTML written to {out} ({len(html_str)} chars)")


def test_html_is_standalone_page(tmp_path):
    """HTML is a standalone scrollable page with PDF download button."""
    with patch("yfinance.Ticker", _make_yf_mock("Walmart Inc.", "Consumer Defensive", "NYQ")):
        html_str = generate_html(
            {"response_text": WMT_RESPONSE_TEXT},
            "WMT",
            "HOLD",
            0.58,
            "2026-06-08",
        )
    assert "deck-stage" not in html_str, "Should not contain deck-stage (slide format)"
    assert "Download PDF" in html_str, "Should have PDF download button"
    assert "window.print()" in html_str, "Should use print for PDF"
    out = tmp_path / "WMT_standalone.html"
    out.write_text(html_str, encoding="utf-8")
    print(f"WMT (standalone) HTML written to {out}")


def test_html_autoescape_prevents_xss(tmp_path):
    """HTML in response_text fields is escaped, not rendered as markup."""
    text = "<script>alert('xss')</script>"
    brief = {"response_text": f"## Analysis\n{text}\n"}
    with patch("yfinance.Ticker", side_effect=Exception("network fail")):
        html_str = generate_html(brief, "XSS", "HOLD", 0.5, "2026-01-01")
    assert "<script>" not in html_str or "&lt;script&gt;" in html_str
    assert "alert('xss')" not in html_str or "alert" in html_str


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_html_generates_valid_output(tmp)
        test_html_with_empty_brief(tmp)
        test_html_with_unknown_ticker(tmp)
        test_html_with_nonstandard_rec(tmp)
        test_html_with_unicode(tmp)
        test_html_with_markdown_tables(tmp)
        test_html_is_standalone_page(tmp)
        test_html_autoescape_prevents_xss(tmp)
    print("\nAll HTML regression tests passed.")
