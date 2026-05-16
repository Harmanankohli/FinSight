import re
import httpx


_TICKER_MAP = None


def _get_sec_ticker_map() -> dict:
    """Fetch and cache SEC EDGAR ticker to company name map."""
    global _TICKER_MAP
    if _TICKER_MAP is not None:
        return _TICKER_MAP
    try:
        resp = httpx.get("https://www.sec.gov/files/company_tickers.json", timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        _TICKER_MAP = {entry["ticker"].upper(): entry.get("title", "")
                       for entry in data.values() if entry.get("ticker")}
        return _TICKER_MAP
    except Exception:
        return {}


def is_valid_ticker(ticker: str) -> bool:
    """Check if ticker exists in SEC EDGAR database."""
    ticker_map = _get_sec_ticker_map()
    return ticker.upper() in ticker_map


def extract_ticker(query: str) -> str:
    """Extract the most likely stock ticker from a natural language query.

    Uses positional heuristics validated against SEC EDGAR:

    1. Words in parentheses — *"Microsoft (MSFT)"* (strongest signal)
    2. Words after trigger prepositions — *"for TSLA"*, *"of AAPL"*, *"buy V"*
    3. Explicit $ prefix — "$V" for Visa, "$AAPL" for Apple
    4. Last 3-5 letter uppercase word — validated against SEC EDGAR
    5. Single-letter with context — validated against SEC EDGAR
    """
    candidates = []

    m = re.search(r"\(([A-Z]{1,5})\)", query)
    if m:
        candidates.append(m.group(1))

    m = re.search(r"(?:for|of|about|buy|sell|invest|in)\s+\$?([A-Z]{1,5})\b", query, re.IGNORECASE)
    if m:
        candidates.append(m.group(1))

    m = re.search(r"\$([A-Z]{1,2})\b", query)
    if m:
        candidates.append(m.group(1))

    matches = re.findall(r"\b([A-Z]{3,5})\b", query)
    if matches:
        candidates.extend(reversed(matches))

    words = query.upper().split()
    for i, word in enumerate(words):
        if len(word) == 1 and i > 0:
            prev = words[i - 1]
            if prev in {"BUY", "SELL", "INVEST", "IN", "FOR", "ABOUT", "ANALYZE"}:
                candidates.append(word)

    ticker_map = _get_sec_ticker_map()
    for candidate in candidates:
        if candidate.upper() in ticker_map:
            return candidate

    return ""
