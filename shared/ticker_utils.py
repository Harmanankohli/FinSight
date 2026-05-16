import re

_STOCK_TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")

_FINANCIAL_STOP_WORDS: frozenset[str] = frozenset({
    "SEC", "EPS", "CEO", "CFO", "CTO", "COO", "CIO", "ROI", "ROE", "ROA",
    "NYSE", "NASDAQ", "INC", "LLC", "LTD", "CORP", "GAAP", "EBIT", "EBITDA",
    "PE", "PEG", "YOY", "Q1", "Q2", "Q3", "Q4", "FY", "YTD", "ATM",
    "IPO", "SPO", "RSU", "ESG", "AI", "RAG", "MCP", "A2A", "API",
})


def is_valid_ticker_format(ticker: str) -> bool:
    return bool(_STOCK_TICKER_RE.match(ticker))


def _is_financial_stop_word(word: str) -> bool:
    return word.upper() in _FINANCIAL_STOP_WORDS


_QUERY_NOISE_WORDS: frozenset[str] = frozenset({
    "analyze", "research", "analyze", "get", "find", "show", "tell", "give",
    "me", "about", "for", "the", "a", "an", "of", "in", "on", "at", "to",
    "from", "with", "by", "and", "or", "but", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can", "shall",
    "please", "need", "want", "like", "use", "using", "used",
    "review", "check", "see", "look", "make", "provide", "perform",
    "analysis", "data", "info", "information", "report", "update",
    "recent", "latest", "current", "last", "this", "that", "these", "those",
    "performance", "financial", "filing", "filings", "document", "documents",
    "stock", "stocks", "share", "shares", "price", "value", "market",
    "sec", "edgar", "news", "sentiment", "risk", "risks",
    "do", "any", "all", "some", "each", "every", "both", "no", "not",
    "only", "just", "very", "really", "quite", "so", "too",
})


def clean_query_for_resolution(text: str) -> str:
    words = text.split()
    cleaned = [w for w in words if w.lower() not in _QUERY_NOISE_WORDS and not _is_financial_stop_word(w)]
    return " ".join(cleaned)


def extract_ticker(query: str) -> str:
    m = re.search(r"\(([A-Z]{1,5})\)", query)
    if m:
        return m.group(1)

    m = re.search(r"(?:for|of|about|buy|sell|invest|in)\s+\$?([A-Z]{1,5})\b", query, re.IGNORECASE)
    if m and m.group(1).isupper():
        return m.group(1)

    m = re.search(r"\$([A-Z]{1,2})\b", query)
    if m:
        return m.group(1)

    matches = [w for w in re.findall(r"\b([A-Z]{3,5})\b", query) if not _is_financial_stop_word(w)]
    if matches:
        return matches[0]

    matches = [w for w in re.findall(r"\b([A-Z]{2})\b", query) if not _is_financial_stop_word(w)]
    if matches:
        return matches[-1]

    return ""
