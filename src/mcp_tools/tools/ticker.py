"""Ticker resolution MCP tools: validate_ticker, resolve_company_ticker + helpers."""

from __future__ import annotations

import asyncio
import logging
import re as _re_module
from urllib.parse import quote as urlquote

from langfuse import observe

from mcp_tools._app import app
from mcp_tools.tools.edgar import _edgar, _prewarm_ticker_map

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Ticker Resolution (company name → ticker)
# ──────────────────────────────────────────────

_REVERSE_INDEX: list[tuple[str, str, str]] | None = None
_REVERSE_INDEX_LOCK = asyncio.Lock()

# Most NYSE/Nasdaq tickers are 1-5 uppercase letters; 1-2 letter suffix for
# share classes (BRK.A, BF.B). Rejects crypto, currency pairs, indices.
_STOCK_TICKER_RE = _re_module.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")


def _normalize_company_name(name: str) -> str:
    """Strip corporate suffixes and punctuation so "Apple Inc." -> "apple" matches "Apple"."""
    s = name.lower().strip()
    for _suffix in [
        " inc",
        " inc.",
        " incorporated",
        " corp",
        " corp.",
        " corporation",
        " ltd",
        " ltd.",
        " limited",
        " llc",
        " lp",
        " plc",
        " company",
        " co.",
        " co",
        " holdings",
        " holding",
        " technologies",
        " technology",
        " group",
        " (the)",
        " (new)",
        " (de)",
        " (md)",
        " (mn)",
    ]:
        if s.endswith(_suffix):
            s = s[: -len(_suffix)].strip()
    s = _re_module.sub(r"[^a-z0-9 ]", "", s)
    s = _re_module.sub(r"\s+", " ", s).strip()
    return s


async def _build_reverse_index() -> list[tuple[str, str, str]]:
    """Build (normalised_name, ticker, raw_title) index lazily with double-checked lock."""
    global _REVERSE_INDEX
    if _REVERSE_INDEX is not None:
        return _REVERSE_INDEX
    async with _REVERSE_INDEX_LOCK:
        if _REVERSE_INDEX is not None:
            return _REVERSE_INDEX
        await _prewarm_ticker_map()
        idx: list[tuple[str, str, str]] = []
        for ticker, title in (_edgar._title_map or {}).items():
            norm = _normalize_company_name(title)
            idx.append((norm, ticker, title))
        # Sort longest-first so prefix matching prefers the most specific name.
        idx.sort(key=lambda x: -len(x[0]))
        _REVERSE_INDEX = idx
        logger.info("Built reverse index with %d entries", len(idx))
        return _REVERSE_INDEX


async def _ticker_sec_lookup(query: str) -> dict | None:
    """Match company name against the SEC ticker map with priority: exact→prefix→word_overlap→substring."""  # noqa: E501
    norm_query = _normalize_company_name(query)
    if not norm_query:
        return None
    idx = await _build_reverse_index()
    query_words = set(norm_query.split())

    best: tuple[int, str, str, str] | None = None
    for norm_title, ticker, title in idx:
        # Priority 1: exact normalised name match (e.g. "JPMorgan Chase" -> JPM).
        if norm_title == norm_query:
            return {
                "ticker": ticker,
                "company_name": title,
                "source": "sec",
                "match": "exact",
            }
        # Priority 2: prefix match -- "Micro" matches "Microsoft Corp" (shortest wins).
        if norm_title.startswith(norm_query):
            score = len(norm_title)
            if best is None or score < best[0]:
                best = (score, ticker, title, "prefix")
        # Priority 3: all query words appear in the title (any order).
        if not query_words:
            continue
        title_words = set(norm_title.split())
        overlap = query_words & title_words
        if overlap and overlap == query_words:
            score = len(norm_title)
            if best is None or score < best[0]:
                best = (score, ticker, title, "word_overlap")

    if best:
        return {
            "ticker": best[1],
            "company_name": best[2],
            "source": "sec",
            "match": best[3],
        }

    # Priority 4: fallback — single-word substring / prefix of the longest query word.
    for word in sorted(query_words, key=len, reverse=True):
        for norm_title, ticker, title in idx:
            if norm_title.startswith(word):
                return {
                    "ticker": ticker,
                    "company_name": title,
                    "source": "sec",
                    "match": f"word_prefix:{word}",
                }
            if word in norm_title.split():
                return {
                    "ticker": ticker,
                    "company_name": title,
                    "source": "sec",
                    "match": f"word_in:{word}",
                }
    return None


def _is_plausible_ticker(symbol: str) -> bool:
    return bool(_STOCK_TICKER_RE.match(symbol))


async def _is_sec_ticker(symbol: str) -> bool:
    await _prewarm_ticker_map()
    return symbol.upper() in (_edgar._title_map or {})


async def _ticker_yahoo_search(query: str) -> dict | None:
    """Fallback ticker resolution via Yahoo Finance search API.

    Three-pass preference order: major US exchange → SEC-registered → any plausible ticker.
    """
    try:
        c = await _edgar._get_client()
        url = (
            f"https://query1.finance.yahoo.com/v1/finance/search"
            f"?q={urlquote(query)}&lang=en-US&region=US&quotesCount=5"
        )
        resp = await c.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        resp.raise_for_status()
        data = resp.json()
        quotes = data.get("quotes", [])

        # Pass 1: prefer tickers listed on NYSE/Nasdaq major exchanges.
        for q in quotes:
            symbol = q.get("symbol", "")
            if not symbol or not _is_plausible_ticker(symbol):
                continue
            if q.get("exchange", "") in ("NYQ", "NMS", "NGM", "ASE", "PCX", "OQA", "OQB"):
                return {
                    "ticker": symbol,
                    "company_name": q.get("longname", q.get("shortname", symbol)),
                    "source": "yfinance",
                }

        # Pass 2: any SEC-registered ticker (broader net than major exchanges).
        for q in quotes:
            symbol = q.get("symbol", "")
            if not symbol or not _is_plausible_ticker(symbol):
                continue
            if await _is_sec_ticker(symbol):
                return {
                    "ticker": symbol,
                    "company_name": q.get("longname", q.get("shortname", symbol)),
                    "source": "yfinance",
                }

        # Pass 3: any ticker that looks plausible (last resort).
        for q in quotes:
            symbol = q.get("symbol", "")
            if symbol and _is_plausible_ticker(symbol):
                return {
                    "ticker": symbol,
                    "company_name": q.get("longname", q.get("shortname", symbol)),
                    "source": "yfinance",
                }

    except Exception as exc:
        logger.warning("Yahoo Finance search failed for %s: %s", query, exc)
    return None


# ──────────────────────────────────────────────
# Ticker MCP Tools
# ──────────────────────────────────────────────


@app.tool()
@observe()
async def validate_ticker(ticker: str) -> dict:
    """Validate a stock ticker against SEC EDGAR database.

    Args:
        ticker: Stock ticker symbol (e.g. AAPL, MSFT, JPM)

    Returns:
        dict with keys: valid (bool), ticker, company_name, cik, or error
    """
    try:
        await _prewarm_ticker_map()
        company_title = await _edgar.get_company_title(ticker.upper())
        if company_title:
            cik = await _edgar._lookup_cik(ticker.upper())
            return {
                "valid": True,
                "ticker": ticker.upper(),
                "company_name": company_title,
                "cik": cik,
            }
        return {
            "valid": False,
            "ticker": ticker.upper(),
            "error": "Ticker not found in SEC database",
        }
    except Exception as exc:
        return {"valid": False, "ticker": ticker.upper(), "error": str(exc)}


@app.tool()
@observe()
async def resolve_company_ticker(text: str) -> dict:
    """Resolve a company name or natural language text to a stock ticker symbol.

    Tries SEC EDGAR reverse lookup first (local, instant), then Yahoo Finance.

    Args:
        text: Company name (e.g. "Mastercard", "Apple Inc", "JPMorgan Chase")

    Returns:
        dict with keys: ticker, company_name, source (sec/yfinance), match, or error
    """
    try:
        # Two-tier: SEC (instant, local) → Yahoo (network, broader coverage).
        result = await _ticker_sec_lookup(text)
        if result:
            return result
        result = await _ticker_yahoo_search(text)
        if result:
            return result
        return {"error": f"Could not resolve '{text}' to a stock ticker"}
    except Exception as exc:
        return {"error": f"Ticker resolution failed: {exc}"}
