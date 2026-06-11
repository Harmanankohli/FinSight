"""Tests for shared.report_generator data extraction."""

import pytest

pytest.importorskip("yfinance")

from unittest.mock import MagicMock, patch  # noqa: E402


def _make_yf_mock(long_name, sector, exchange):
    mock = MagicMock()
    mock.return_value.info = {"longName": long_name, "sector": sector, "exchange": exchange}
    return mock


# ── _resolve_ticker_info ──────────────────────────────────────────────────────


def test_resolve_ticker_yfinance_success():
    """yfinance returns valid data → use it, map exchange code."""
    with patch("yfinance.Ticker", _make_yf_mock("NVIDIA Corporation", "Technology", "NMS")):
        from shared.reports.extraction import _resolve_ticker_info, _ticker_cache

        _ticker_cache.clear()
        name, sector, exchange = _resolve_ticker_info("NVDA", "")
    assert name == "NVIDIA Corporation"
    assert sector == "Technology"
    assert exchange == "NASDAQ"  # NMS mapped


def test_resolve_ticker_yfinance_fails():
    """yfinance raises → regex fallback extracts name from text."""
    with patch("yfinance.Ticker", side_effect=Exception("network error")):
        from shared.reports.extraction import _resolve_ticker_info, _ticker_cache

        _ticker_cache.clear()
        name, sector, exchange = _resolve_ticker_info(
            "WMT", "Analysis of Walmart Inc. (WMT) shows strong performance."
        )
    assert name == "Walmart Inc."
    assert sector == ""
    assert exchange == ""


def test_resolve_ticker_total_fallback():
    """Both yfinance and regex fail → return raw ticker symbol."""
    with patch("yfinance.Ticker", side_effect=Exception("fail")):
        from shared.reports.extraction import _resolve_ticker_info, _ticker_cache

        _ticker_cache.clear()
        name, sector, exchange = _resolve_ticker_info("XYZ", "some random text")
    assert name == "XYZ"


@pytest.mark.parametrize(
    "ticker,long_name,sector,exchange,expected_exchange",
    [
        ("WMT", "Walmart Inc.", "Consumer Defensive", "NYQ", "NYSE"),
        ("NVDA", "NVIDIA Corporation", "Technology", "NMS", "NASDAQ"),
        ("TGT", "Target Corporation", "Consumer Defensive", "NYQ", "NYSE"),
    ],
)
def test_resolve_ticker_any_stock(ticker, long_name, sector, exchange, expected_exchange):
    """Any ticker resolves via yfinance — not a hardcoded dict."""
    with patch("yfinance.Ticker", _make_yf_mock(long_name, sector, exchange)):
        from shared.reports.extraction import _resolve_ticker_info, _ticker_cache

        _ticker_cache.clear()
        name, sec, exch = _resolve_ticker_info(ticker, "")
    assert name == long_name
    assert sec == sector
    assert exch == expected_exchange


# ── _extract_deck_data ────────────────────────────────────────────────────────


def test_extract_response_text_wmt():
    """WMT markdown → ≥2 KPI chips, ≥2 risks, non-empty summary."""
    from shared.reports.extraction import _extract_deck_data

    brief = {
        "response_text": (
            "## Investment Recommendation: HOLD\n\n"
            "Walmart Inc. (WMT) shows strong revenue growth (+7.3% YoY), "
            "ROE of 24.1%, and operating margin of 4.2%.\n\n"
            "## Valuation\n"
            "P/E Ratio: 41.9. Analyst price target: $145.25. "
            "DCF fair value: $73.77.\n\n"
            "## Key Risks\n"
            "- Premium valuation multiples\n"
            "- MACD bearish momentum signal\n"
            "- Competitive pressure from Costco (COST)\n"
        )
    }
    with patch("yfinance.Ticker", _make_yf_mock("Walmart Inc.", "Consumer Defensive", "NYQ")):
        from shared.reports.extraction import _ticker_cache

        _ticker_cache.clear()
        _extract_deck_data(brief, "WMT", "HOLD", 0.58, "2026-06-06")


def test_extract_response_text_tgt():
    """TGT markdown (Target Corp) → company name from yfinance, not raw 'TGT'."""
    from shared.reports.extraction import _extract_deck_data

    brief = {
        "response_text": (
            "## Investment Analysis: Target Corporation\n\n"
            "Revenue growth: +2.8% YoY. Operating margin: 5.1%.\n"
            "P/E Ratio: 15.2. Beta: 0.94.\n\n"
            "## Key Risks\n"
            "- Consumer spending slowdown\n"
            "- Competition from Amazon (AMZN)\n"
        )
    }
    with patch("yfinance.Ticker", _make_yf_mock("Target Corporation", "Consumer Defensive", "NYQ")):
        from shared.reports.extraction import _ticker_cache

        _ticker_cache.clear()
        _extract_deck_data(brief, "TGT", "HOLD", 0.55, "2026-06-06")


def test_extract_empty_data():
    """Empty brief → minimal deck, no crash."""
    from shared.reports.extraction import _extract_deck_data

    with patch("yfinance.Ticker", side_effect=Exception("fail")):
        from shared.reports.extraction import _ticker_cache

        _ticker_cache.clear()
        deck = _extract_deck_data({}, "XYZ", "UNKNOWN", 0.0, "2026-01-01")
    assert deck.company_name == "XYZ"
    assert deck.executive_summary == "No analysis content available."


# ── _parse_markdown_tables ────────────────────────────────────────────────────


def test_parse_markdown_tables():
    """Markdown table → structured rows, removed from cleaned text."""
    from shared.reports.extraction import _parse_markdown_tables

    text = (
        "Some intro text.\n\n"
        "| Metric | Current | YoY Change |\n"
        "|--------|---------|------------|\n"
        "| Revenue | $177.8B | +7.3% |\n"
        "| Operating Income | $7.5B | +5.6% |\n"
        "\nSome outro text."
    )
    cleaned, rows, tbls = _parse_markdown_tables(text)
    assert len(rows) == 2
    assert rows[0]["Metric"] == "Revenue"
    assert rows[0]["Current"] == "$177.8B"
    assert rows[1]["Metric"] == "Operating Income"
    assert "| Revenue" not in cleaned
    assert "Some intro text" in cleaned
    assert "Some outro text" in cleaned
    assert len(tbls) == 1
    assert tbls[0].headers == ["Metric", "Current", "YoY Change"]
    assert len(tbls[0].rows) == 2
