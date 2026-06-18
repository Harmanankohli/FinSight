import json
import re

_STOCK_TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")

# Common financial/tech acronyms that look like tickers but aren't; prevents
# false-positive extraction from queries mentioning "SEC", "EPS", "AI", etc.
_FINANCIAL_STOP_WORDS: frozenset[str] = frozenset(
    {
        "SEC",
        "EPS",
        "CEO",
        "CFO",
        "CTO",
        "COO",
        "CIO",
        "ROI",
        "ROE",
        "ROA",
        "NYSE",
        "NASDAQ",
        "INC",
        "LLC",
        "LTD",
        "CORP",
        "GAAP",
        "EBIT",
        "EBITDA",
        "PE",
        "PEG",
        "YOY",
        "Q1",
        "Q2",
        "Q3",
        "Q4",
        "FY",
        "YTD",
        "ATM",
        "IPO",
        "SPO",
        "RSU",
        "ESG",
        "AI",
        "RAG",
        "MCP",
        "A2A",
        "API",
        # Common English words that collide with rare tickers
        "I",
        "AM",
        "AN",
        "AS",
        "AT",
        "BE",
        "BY",
        "DO",
        "GO",
        "IF",
        "IN",
        "IS",
        "IT",
        "ME",
        "MY",
        "NO",
        "OF",
        "OK",
        "ON",
        "OR",
        "SO",
        "TO",
        "UP",
        "US",
        "WE",
        "VS",
        "RE",
        "HI",
        "IM",
        "OH",
    }
)


def is_valid_ticker_format(ticker: str) -> bool:
    return bool(_STOCK_TICKER_RE.match(ticker))


def _is_financial_stop_word(word: str) -> bool:
    return word.upper() in _FINANCIAL_STOP_WORDS


# Low-information words stripped before passing text to ticker-resolution tools,
# reducing ambiguity in the NLP-based company→ticker lookup.
_QUERY_NOISE_WORDS: frozenset[str] = frozenset(
    {
        "analyze",
        "research",
        "get",
        "find",
        "show",
        "tell",
        "give",
        "me",
        "about",
        "for",
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "at",
        "to",
        "from",
        "with",
        "by",
        "and",
        "or",
        "but",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "shall",
        "please",
        "need",
        "want",
        "like",
        "use",
        "using",
        "used",
        "review",
        "check",
        "see",
        "look",
        "make",
        "provide",
        "perform",
        "analysis",
        "data",
        "info",
        "information",
        "report",
        "update",
        "recent",
        "latest",
        "current",
        "last",
        "this",
        "that",
        "these",
        "those",
        "performance",
        "financial",
        "filing",
        "filings",
        "document",
        "documents",
        "stock",
        "stocks",
        "share",
        "shares",
        "price",
        "value",
        "market",
        "sec",
        "edgar",
        "news",
        "sentiment",
        "risk",
        "risks",
        "any",
        "all",
        "some",
        "each",
        "every",
        "both",
        "no",
        "not",
        "only",
        "just",
        "very",
        "really",
        "quite",
        "so",
        "too",
    }
)


def clean_query_for_resolution(text: str) -> str:
    words = text.split()
    cleaned = [
        w for w in words if w.lower() not in _QUERY_NOISE_WORDS and not _is_financial_stop_word(w)
    ]
    return " ".join(cleaned)


# Priority order: (1) parenthesised tickers "buy NVDA (NVDA)",
# (2) preposition-adjacent "buy NVDA" or "invest in NVDA",
# (3) $ prefix as explicit signal, (4) isolated uppercase words,
# (5) case-insensitive fallback for lowercase tickers like "aapl".
def extract_ticker(query: str) -> str:
    m = re.search(r"\(([A-Z]{1,5}(?:\.[A-Z]{1,2})?)\)", query)
    if m and not _is_financial_stop_word(m.group(1)):
        return m.group(1)

    m = re.search(r"\b([A-Z]{1,5})\s+\([A-Za-z][a-z]", query)
    if m and not _is_financial_stop_word(m.group(1)):
        return m.group(1)

    m = re.search(
        r"(?:for|of|about|buy|sell|invest|in)\s+\$?([A-Z]{1,5}(?:\.[A-Z]{1,2})?)\b",
        query,
    )
    if m and not _is_financial_stop_word(m.group(1)):
        return m.group(1)

    m = re.search(r"\$([A-Z]{1,5}(?:\.[A-Z]{1,2})?)\b", query)
    if m:
        return m.group(1)

    matches = [w for w in re.findall(r"\b([A-Z]{3,5})\b", query) if not _is_financial_stop_word(w)]
    if matches:
        return matches[0]

    matches = [w for w in re.findall(r"\b([A-Z]{1,2})\b", query) if not _is_financial_stop_word(w)]
    if matches:
        return matches[-1]

    # Case-insensitive fallback: match lowercase/mixed-case ticker after a
    # preposition or action verb (e.g. "analyze aapl", "about nvda").
    m = re.search(
        r"(?:for|of|about|buy|sell|invest|in|analyze|research)\s+\$?([A-Za-z]{1,5}(?:\.[A-Za-z]{1,2})?)\b",
        query,
        re.IGNORECASE,
    )
    if m:
        candidate = m.group(1).upper()
        if (
            is_valid_ticker_format(candidate)
            and not _is_financial_stop_word(candidate)
            and candidate.lower() not in _QUERY_NOISE_WORDS
        ):
            return candidate

    # Bare lowercase ticker (e.g. just "nvda" or "aapl" as the entire query)
    stripped = query.strip()
    if re.fullmatch(r"[A-Za-z]{1,5}(\.[A-Za-z]{1,2})?", stripped):
        candidate = stripped.upper()
        if (
            is_valid_ticker_format(candidate)
            and not _is_financial_stop_word(candidate)
            and candidate.lower() not in _QUERY_NOISE_WORDS
        ):
            return candidate

    return ""


_HOLDINGS_PATTERNS = [
    re.compile(
        r"(?:portfolio|holdings?)\s*(?::|holds?|contains?|includes?|consists?\s+of)\s*(\b[A-Z]{1,5}\b(?:\s*,\s*\b[A-Z]{1,5}\b)*(?:\s+(?:and|&)\s*\b[A-Z]{1,5}\b)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:I\s+(?:own|hold|have|am\s+invested\s+in)|my\s+(?:current\s+)?(?:portfolio|holdings?|positions?))\s+(?:include|consist)\s+(?:of\s+)?(?:the\s+)?(?:following\s+)?(?:tickers?\s*:?\s*)?(\b[A-Z]{1,5}\b(?:\s*,\s*\b[A-Z]{1,5}\b)*(?:\s+(?:and|&)\s*\b[A-Z]{1,5}\b)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:my\s+(?:current\s+)?(?:portfolio|holdings?|positions?))\s+are\s+(\b[A-Z]{1,5}\b(?:\s*,\s*\b[A-Z]{1,5}\b)*(?:\s+(?:and|&)\s*\b[A-Z]{1,5}\b)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:I|we)\s+(?:currently\s+)?(?:own|hold|have)\s*:?\s*(\b[A-Z]{1,5}\b(?:\s*,\s*\b[A-Z]{1,5}\b)*(?:\s+(?:and|&)\s*\b[A-Z]{1,5}\b)?)",
        re.IGNORECASE,
    ),
]


# Delegates to the MCP server's SEC-sourced database for authoritative ticker
# validation. The server queries EDGAR to confirm existence and return canonical form.
async def validate_ticker_via_mcp(mcp, ticker: str) -> tuple[bool, str, str]:
    """Call MCP validate_ticker. Returns (valid, canonical_ticker, company_name_or_error). Raises on MCP failure."""  # noqa: E501
    result = await mcp.call_tool_by_name("validate_ticker", {"ticker": ticker})
    if hasattr(result, "content"):
        for item in result.content:
            try:
                v = json.loads(item.text if hasattr(item, "text") else str(item))
                if v.get("valid"):
                    return True, v.get("ticker", ticker), v.get("company_name", "")
                return False, ticker, v.get("error", "Ticker not found in SEC database")
            except Exception:
                continue
    return False, ticker, "Ticker not found in SEC database"


# Delegates company-name→ticker resolution to the MCP server, which queries
# SEC EDGAR. Strips noise words first to improve NLP accuracy server-side.
async def resolve_ticker_via_mcp(mcp, query: str, exclude_ticker: str = "") -> tuple[str, str]:
    """Call MCP resolve_company_ticker. Returns (ticker, company_name). Raises on MCP failure."""
    cleaned = clean_query_for_resolution(query)
    if exclude_ticker:
        cleaned = cleaned.replace(exclude_ticker, "").strip()
    if not cleaned:
        cleaned = query
    result = await mcp.call_tool_by_name("resolve_company_ticker", {"text": cleaned})
    if hasattr(result, "content"):
        for item in result.content:
            try:
                v = json.loads(item.text if hasattr(item, "text") else str(item))
                ticker = v.get("ticker", "")
                if ticker and is_valid_ticker_format(ticker):
                    return ticker, v.get("company_name", "")
            except Exception:
                continue
    return "", ""


async def validate_ticker(ticker: str) -> tuple[bool, str, str]:
    """Module-level wrapper: validates ticker via shared MCP singleton.

    Returns (valid, canonical_ticker, company_or_error).
    On MCP failure, optimistically returns (True, ticker, "") so callers degrade gracefully.
    """
    if not ticker:
        return (
            False,
            "",
            "Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V).",  # noqa: E501
        )
    try:
        from shared.mcp_client import get_shared_mcp

        mcp = await get_shared_mcp()
        return await validate_ticker_via_mcp(mcp, ticker)
    except Exception as e:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "MCP ticker validation failed, proceeding with regex guess: %s", e
        )
        return True, ticker, ""


async def resolve_ticker(query: str, exclude: str = "") -> tuple[str, str]:
    """Module-level wrapper: resolves company name → ticker via shared MCP singleton.

    Returns (ticker, company_name). Returns ("", "") on failure.
    """
    try:
        from shared.mcp_client import get_shared_mcp

        mcp = await get_shared_mcp()
        return await resolve_ticker_via_mcp(mcp, query, exclude)
    except Exception as e:
        import logging as _logging

        _logging.getLogger(__name__).warning("MCP ticker resolution failed: %s", e)
        return "", ""


async def resolve_and_validate_ticker(query: str) -> tuple[str | None, str | None]:
    """Extract, resolve, and validate a stock ticker from a query.

    Returns (validated_ticker, company_or_error_message).
    - validated_ticker: canonical ticker on success (None on failure)
    - company_or_error_message: company name on success, error message on failure

    Pipeline: extract_ticker -> resolve_ticker (if missing) -> validate_ticker ->
              resolve_ticker (if invalid) -> validate_ticker -> result
    """
    ticker = extract_ticker(query)
    resolved = False

    if not ticker:
        ticker, _ = await resolve_ticker(query)
        resolved = True

    if not ticker:
        return None, "Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V)."

    valid, validated_ticker, company = await validate_ticker(ticker)
    if not valid and not resolved:
        ticker, _ = await resolve_ticker(query, ticker)
        if ticker:
            valid, validated_ticker, company = await validate_ticker(ticker)

    if not valid:
        return None, f"Ticker '{ticker}' is not valid. Error: {company}"

    return validated_ticker, company or validated_ticker


def extract_holdings(query: str, exclude_ticker: str = "") -> list[str]:
    for pattern in _HOLDINGS_PATTERNS:
        m = pattern.search(query)
        if m:
            raw = m.group(1)
            tickers = re.split(r"\s*(?:,|and|&)\s*", raw, flags=re.IGNORECASE)
            result = [
                t.strip().upper()
                for t in tickers
                if t.strip()
                and is_valid_ticker_format(t.strip().upper())
                and not _is_financial_stop_word(t.strip().upper())
                and t.strip().lower() not in _QUERY_NOISE_WORDS
            ]
            if exclude_ticker:
                result = [t for t in result if t != exclude_ticker]
            return result
    return []
