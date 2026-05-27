import pytest
from shared.ticker_utils import (
    is_valid_ticker_format,
    extract_ticker,
    clean_query_for_resolution,
    extract_holdings,
)


# ── is_valid_ticker_format ────────────────────────────────────────────────────

@pytest.mark.parametrize("ticker,expected", [
    ("AAPL", True),
    ("NVDA", True),
    ("BRK.A", True),
    ("V", True),
    ("GOOGL", True),
    ("aapl", False),
    ("TOOLONG", False),  # 7 chars > max 5
    ("123", False),
    ("", False),
    ("BRK.ABC", False),  # suffix > 2 chars
])
def test_is_valid_ticker_format(ticker, expected):
    assert is_valid_ticker_format(ticker) == expected


# ── extract_ticker ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("query,expected", [
    ("Analyze AAPL for me", "AAPL"),
    ("Buy $TSLA today", "TSLA"),
    ("NVIDIA (NVDA) earnings report", "NVDA"),
    ("V (Visa) stock analysis", "V"),
    ("invest in MSFT", "MSFT"),
    ("research SEC filings for the company", ""),   # SEC is stop word
    ("what are the EPS projections", ""),            # EPS is stop word
    ("how does AI perform in finance", ""),          # AI is stop word
])
def test_extract_ticker(query, expected):
    assert extract_ticker(query) == expected


def test_extract_ticker_dollar_prefix_beats_bare_word():
    assert extract_ticker("should I buy $AMZN") == "AMZN"


def test_extract_ticker_parens_ticker_beats_standalone():
    # Parenthesised ticker takes highest priority
    assert extract_ticker("Apple Inc (AAPL) is trading higher") == "AAPL"


# ── clean_query_for_resolution ────────────────────────────────────────────────

def test_clean_query_strips_noise_words():
    query = "please analyze the stock data for Apple"
    cleaned = clean_query_for_resolution(query)
    assert "Apple" in cleaned
    assert "please" not in cleaned.lower()
    assert "analyze" not in cleaned.lower()


def test_clean_query_preserves_company_names():
    query = "Microsoft earnings report"
    cleaned = clean_query_for_resolution(query)
    assert "Microsoft" in cleaned


def test_clean_query_empty_after_all_noise():
    # All noise words → cleaned should be empty or just the remaining content
    query = "please get me the data"
    cleaned = clean_query_for_resolution(query)
    # "please", "get", "me", "the", "data" are all noise words
    assert cleaned.strip() == ""


# ── extract_holdings ──────────────────────────────────────────────────────────

def test_extract_holdings_comma_separated():
    query = "My portfolio holds AAPL, MSFT, GOOG"
    holdings = extract_holdings(query)
    assert "AAPL" in holdings
    assert "MSFT" in holdings
    assert "GOOG" in holdings


def test_extract_holdings_with_and():
    query = "I own TSLA and NVDA"
    holdings = extract_holdings(query)
    assert "TSLA" in holdings
    assert "NVDA" in holdings


def test_extract_holdings_excludes_ticker():
    query = "My portfolio holds AAPL, MSFT, NVDA"
    holdings = extract_holdings(query, exclude_ticker="AAPL")
    assert "AAPL" not in holdings
    assert "MSFT" in holdings
    assert "NVDA" in holdings


def test_extract_holdings_empty_when_no_match():
    assert extract_holdings("What is the weather today?") == []
