from __future__ import annotations

import asyncio
import ast
import json
import logging
import os
import sys
import subprocess

if sys.platform != "win32":
    import resource
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote as urlquote

import feedparser
import httpx
import numpy as np
import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP
from sentence_transformers import SentenceTransformer
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = logging.getLogger(__name__)
app = FastMCP("finsight-mcp")

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8010"))
AGENT_CARDS_DIR = Path(__file__).resolve().parent.parent / "agent_cards"
EMBED_MODEL_NAME = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")


# ──────────────────────────────────────────────
# Lazy Agent Registry (no model download at import time)
# ──────────────────────────────────────────────

_registry_lock = asyncio.Lock()
_registry_ready = False
_model_embed: SentenceTransformer | None = None
_df_registry: pd.DataFrame = pd.DataFrame(
    columns=["card_uri", "agent_card", "card_embeddings"]
)


def _load_agent_cards() -> tuple[list[str], list[dict]]:
    """Load agent card JSON files from AGENT_CARDS_DIR synchronously (called once)."""
    card_uris, agent_cards = [], []
    if not AGENT_CARDS_DIR.is_dir():
        logger.warning("Agent cards directory not found: %s", AGENT_CARDS_DIR)
        return card_uris, agent_cards
    for filename in sorted(os.listdir(AGENT_CARDS_DIR)):
        if filename.lower().endswith(".json"):
            file_path = AGENT_CARDS_DIR / filename
            if file_path.is_file():
                try:
                    with file_path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    stem = Path(filename).stem
                    card_uris.append(f"resource://agent_cards/{stem}")
                    agent_cards.append(data)
                except Exception as exc:
                    logger.error("Error loading %s: %s", filename, exc)
    logger.info("Loaded %d agent cards", len(agent_cards))
    return card_uris, agent_cards


async def _ensure_registry() -> None:
    """Initialise the embedding model and registry DataFrame on first use."""
    global _registry_ready, _model_embed, _df_registry
    if _registry_ready:
        return
    async with _registry_lock:
        if _registry_ready:
            return
        loop = asyncio.get_running_loop()

        def _init():
            card_uris, agent_cards = _load_agent_cards()
            if not agent_cards:
                return None, pd.DataFrame(
                    columns=["card_uri", "agent_card", "card_embeddings"]
                )
            model = SentenceTransformer(EMBED_MODEL_NAME)
            df = pd.DataFrame({"card_uri": card_uris, "agent_card": agent_cards})
            df["card_embeddings"] = df["agent_card"].apply(
                lambda c: model.encode(json.dumps(c))
            )
            return model, df

        _model_embed, _df_registry = await loop.run_in_executor(None, _init)
        _registry_ready = True


# ──────────────────────────────────────────────
# Agent Registry Tools
# ──────────────────────────────────────────────

@app.tool(
    name="find_agent",
    description="Finds the most relevant agent card based on a natural language query string",
)
async def find_agent(query: str) -> str:
    await _ensure_registry()
    if _df_registry.empty or _model_embed is None:
        return json.dumps({"error": "No agent cards loaded"})
    loop = asyncio.get_running_loop()

    def _search():
        q_emb = _model_embed.encode(query)
        dots = np.dot(np.stack(_df_registry["card_embeddings"]), q_emb)
        return int(np.argmax(dots))

    best_idx = await loop.run_in_executor(None, _search)
    return json.dumps(_df_registry.iloc[best_idx]["agent_card"])


@app.resource("resource://agent_cards/list", mime_type="application/json")
async def get_agent_cards() -> dict:
    await _ensure_registry()
    return {
        "agent_cards": _df_registry["card_uri"].to_list()
        if not _df_registry.empty
        else []
    }


@app.resource("resource://agent_cards/{card_name}", mime_type="application/json")
async def get_agent_card(card_name: str) -> dict:
    await _ensure_registry()
    if _df_registry.empty:
        return {"agent_card": None}
    uri = f"resource://agent_cards/{card_name}"
    cards = _df_registry.loc[
        _df_registry["card_uri"] == uri, "agent_card"
    ].to_list()
    return {"agent_card": cards[0]} if cards else {"agent_card": None}


# ──────────────────────────────────────────────
# yfinance Tools
# ──────────────────────────────────────────────

def _serialise_value(v: Any) -> Any:
    """Recursively convert non-JSON-serialisable types (Timestamp,
    datetime, numpy scalars, NaN/Inf) so the caller never hits a
    serialisation error."""
    if isinstance(v, dict):
        return {_serialise_value(k): _serialise_value(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_serialise_value(i) for i in v]
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, np.ndarray):
        return v.tolist()
    return v


@app.tool()
async def get_prices(ticker: str, period: str = "1y", interval: str = "1d") -> dict:
    """Fetch OHLCV price history data for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        period: Time period. Options: 1d 5d 1mo 3mo 6mo 1y 2y 5y 10y ytd max
        interval: Data interval. Options: 1m 2m 5m 15m 30m 60m 90m 1h 1d 5d 1wk 1mo 3mo

    Returns:
        dict with keys: ticker, period, data (list of OHLCV records with ISO dates)
    """
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period, interval=interval)
        records = _serialise_value(hist.reset_index().to_dict(orient="records"))
        return {"ticker": ticker, "period": period, "data": records}
    except Exception as exc:
        logger.warning("get_prices failed for %s: %s", ticker, exc)
        return {"ticker": ticker, "period": period, "error": str(exc), "data": []}


@app.tool()
async def get_financials(ticker: str) -> dict:
    """Fetch financial statements and company info for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)

    Returns:
        dict with keys: income_statement, balance_sheet, cash_flow, info
    """
    try:
        stock = yf.Ticker(ticker)
        return _serialise_value({
            "income_statement": stock.financials.to_dict()
            if stock.financials is not None
            else {},
            "balance_sheet": stock.balance_sheet.to_dict()
            if stock.balance_sheet is not None
            else {},
            "cash_flow": stock.cashflow.to_dict()
            if stock.cashflow is not None
            else {},
            "info": stock.info or {},
        })
    except Exception as exc:
        logger.warning("get_financials failed for %s: %s", ticker, exc)
        return {
            "ticker": ticker, "error": str(exc),
            "income_statement": {}, "balance_sheet": {}, "cash_flow": {}, "info": {},
        }


@app.tool()
async def get_options_chain(ticker: str, expiration: str | None = None) -> dict:
    """Fetch options chain data for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        expiration: Option expiration date (e.g. 2025-01-17). Omit for available dates.

    Returns:
        dict: calls + puts if expiration given, else expirations list.
    """
    try:
        stock = yf.Ticker(ticker)
        if expiration:
            chain = stock.option_chain(expiration)
            return _serialise_value({
                "calls": chain.calls.to_dict(orient="records"),
                "puts": chain.puts.to_dict(orient="records"),
            })
        return {"expirations": list(stock.options)}
    except Exception as exc:
        logger.warning("get_options_chain failed for %s: %s", ticker, exc)
        return {"ticker": ticker, "error": str(exc)}


# ──────────────────────────────────────────────
# SEC EDGAR Tools
# ──────────────────────────────────────────────

_SEC_HEADERS = {
    "User-Agent": "FinSight Research (contact@finsight.com)",
    "Accept-Encoding": "gzip, deflate",
}


class _EdgarClient:
    """Async SEC EDGAR client with shared client, cached ticker map,
    URL encoding, and parallel array bounds checking."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._cik_cache: dict[str, str] = {}
        self._ticker_map: dict[str, str] | None = None
        self._title_map: dict[str, str] | None = None
        self._ticker_map_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        headers=_SEC_HEADERS, timeout=30.0, follow_redirects=True
                    )
        return self._client

    async def _get_ticker_map(self) -> dict[str, str]:
        if self._ticker_map is not None:
            return self._ticker_map
        async with self._ticker_map_lock:
            if self._ticker_map is not None:
                return self._ticker_map
            c = await self._get_client()
            resp = await c.get("https://www.sec.gov/files/company_tickers.json")
            resp.raise_for_status()
            raw = resp.json()
            self._ticker_map = {
                entry["ticker"]: str(entry["cik_str"]).zfill(10)
                for entry in raw.values()
            }
            self._title_map = {
                entry["ticker"]: entry.get("title", "")
                for entry in raw.values()
            }
        return self._ticker_map

    async def get_company_title(self, ticker: str) -> str:
        await self._get_ticker_map()
        return (self._title_map or {}).get(ticker.upper(), "")

    async def _lookup_cik(self, ticker: str) -> str:
        ticker = ticker.upper()
        if ticker in self._cik_cache:
            return self._cik_cache[ticker]
        mapping = await self._get_ticker_map()
        if ticker not in mapping:
            raise ValueError(f"Ticker {ticker} not found in SEC EDGAR")
        self._cik_cache[ticker] = mapping[ticker]
        return self._cik_cache[ticker]

    async def get_company_filings(
        self,
        ticker: str,
        form_types: list[str] | None = None,
        limit: int = 10,
    ) -> dict:
        try:
            cik = await self._lookup_cik(ticker)
        except Exception as exc:
            return {"ticker": ticker, "error": str(exc), "filings": []}

        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        c = await self._get_client()
        data: dict = {}
        for attempt in range(3):
            try:
                resp = await c.get(url)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.warning(
                        "get_company_filings failed for %s after 3 retries", ticker
                    )
                    return {
                        "ticker": ticker,
                        "error": "Failed after 3 retries",
                        "filings": [],
                    }

        filings = data.get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])
        docs = filings.get("primaryDocument", [])
        acc_nums = filings.get("accessionNumber", [])

        n = min(len(forms), len(dates), len(docs), len(acc_nums))

        _INDEX_ONLY_FORMS = frozenset({"3", "4", "5", "3/A", "4/A", "5/A"})

        cik_short = str(int(cik))
        result = []
        for i in range(n):
            if form_types and forms[i] not in form_types:
                continue
            if len(result) >= limit:
                break

            acc_clean = acc_nums[i].replace("-", "")
            safe_doc = urlquote(docs[i], safe="")

            if forms[i] in _INDEX_ONLY_FORMS or "/" in docs[i]:
                edgar_url = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik_short}/{acc_clean}/{acc_nums[i]}-index.htm"
                )
            else:
                edgar_url = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik_short}/{acc_clean}/{safe_doc}"
                )

            raw_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_short}/{acc_clean}/{safe_doc}"
            )

            result.append({
                "form": forms[i],
                "filing_date": dates[i],
                "description": docs[i],
                "edgar_url": raw_url,
                "ix_url": f"https://www.sec.gov/ix?doc=/Archives/edgar/data/{cik_short}/{acc_clean}/{safe_doc}",
            })
        return {"ticker": ticker, "cik": cik, "filings": result}

    async def full_text_search(
        self, query: str, ticker: str | None = None
    ) -> dict:
        try:
            params: dict = {"q": query}
            if ticker:
                params["cik"] = await self._lookup_cik(ticker)
            c = await self._get_client()
            resp = await c.get(
                "https://efts.sec.gov/LATEST/search-index?dateRange=all",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("full_text_search failed: %s", exc)
            return {"query": query, "error": str(exc), "results": []}

        results = []
        for hit in data.get("hits", {}).get("hits", [])[:10]:
            src = hit.get("_source", {})
            results.append({
                "score": hit["_score"],
                "ticker": src.get("ticker", ""),
                "form": src.get("form", ""),
                "filing_date": src.get("filingDate", ""),
                "description": src.get("description", ""),
                "url": f"https://www.sec.gov{src.get('file_url', '')}",
            })
        return {"query": query, "results": results}

    async def get_filing_content(self, edgar_url: str, ix_url: str | None = None) -> dict:
        """Fetch and extract text content from an SEC EDGAR filing URL.

        Tries multiple approaches to get usable content:
        1. Raw filing document (preferred for non-XBRL filings)
        2. Fallback to ix_url if raw fails
        """
        tried_urls = [edgar_url]
        if ix_url and ix_url != edgar_url:
            tried_urls.append(ix_url)

        for url in tried_urls:
            try:
                c = await self._get_client()
                resp = await c.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "")

                text = ""
                if "json" in content_type.lower():
                    text = json.dumps(resp.json(), indent=2)
                elif "xml" in content_type.lower() or url.endswith((".xml", ".xsd")):
                    try:
                        soup = BeautifulSoup(resp.text, "lxml-xml")
                    except Exception:
                        soup = BeautifulSoup(resp.text, "html.parser")
                    for tag in soup.find_all(["script", "style", "xbrldocument"]):
                        tag.decompose()
                    text = soup.get_text(separator=" ", strip=True)
                else:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for script in soup(["script", "style"]):
                        script.decompose()
                    for tag in soup.find_all(["style", "noscript"]):
                        tag.decompose()
                    text = soup.get_text(separator=" ", strip=True)

                text = " ".join(text.split())
                if len(text) > 50000:
                    text = text[:50000] + "..."
                if len(text.strip()) < 100:
                    logger.info("Content too short, skipping: %s", url)
                    continue
                return {"url": url, "content": text, "length": len(text)}
            except Exception as exc:
                logger.info("Tried %s, got: %s", url, exc)
                continue

        return {"url": edgar_url, "error": "Could not extract content from any URL", "content": ""}


_edgar = _EdgarClient()


@app.tool()
async def get_company_filings(
    ticker: str, form_types: str = "", limit: int = 10
) -> dict:
    """Retrieve SEC filings for a publicly traded company.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        form_types: Comma-separated form types to filter (e.g. "10-K,10-Q,8-K"). Empty = all.
        limit: Maximum number of filings to return (default 10)

    Returns:
        dict with keys: ticker, cik, filings (list of {form, filing_date, description, edgar_url})
    """
    try:
        types_list = (
            [t.strip() for t in form_types.split(",") if t.strip()]
            if form_types
            else None
        )
        return await _edgar.get_company_filings(ticker, types_list, limit)
    except Exception as exc:
        logger.warning("get_company_filings tool failed for %s: %s", ticker, exc)
        return {"ticker": ticker, "error": str(exc), "filings": []}


@app.tool()
async def full_text_search(query: str, ticker: str | None = None) -> dict:
    """Search full text of SEC EDGAR filings.

    Args:
        query: Keywords (e.g. "revenue growth AI semiconductors")
        ticker: Optional ticker to narrow to one company.

    Returns:
        dict with keys: query, results (list of {score, ticker, form, filing_date, description, url})
    """
    try:
        return await _edgar.full_text_search(query, ticker)
    except Exception as exc:
        logger.warning("full_text_search tool failed: %s", exc)
        return {"query": query, "error": str(exc), "results": []}


@app.tool()
async def get_filing_content(edgar_url: str, ix_url: str | None = None) -> dict:
    """Fetch and extract text content from an SEC EDGAR filing URL.

    Use this to get the full text of a 10-K, 10-Q, 8-K, or other SEC filing
    for detailed analysis. The content is extracted from the raw filing document.

    Args:
        edgar_url: The full EDGAR raw filing URL (from get_company_filings result's 'edgar_url' field)
        ix_url: Optional IXBRL viewer URL (from get_company_filings result's 'ix_url' field) as fallback

    Returns:
        dict with keys: url, content (extracted text), length, or error
    """
    try:
        return await _edgar.get_filing_content(edgar_url, ix_url)
    except Exception as exc:
        logger.warning("get_filing_content tool failed: %s", exc)
        return {"url": edgar_url, "error": str(exc), "content": ""}


_prewarm_done = False


async def _prewarm_ticker_map() -> None:
    global _prewarm_done
    if _prewarm_done:
        return
    try:
        await _edgar._get_ticker_map()
        _prewarm_done = True
        logger.info("SEC ticker map pre-warmed")
    except Exception as e:
        logger.warning("SEC ticker map pre-warm failed: %s", e)


@app.tool()
async def validate_ticker(ticker: str) -> dict:
    """Validate a stock ticker against SEC EDGAR database.

    Use this to confirm a ticker is valid before processing. Returns company
    name and CIK if valid, or error if not found.

    Args:
        ticker: Stock ticker symbol (e.g. AAPL, MSFT, MA, V)

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
        return {"valid": False, "ticker": ticker.upper(), "error": "Ticker not found in SEC database"}
    except Exception as exc:
        return {"valid": False, "ticker": ticker.upper(), "error": str(exc)}


# ──────────────────────────────────────────────
# Ticker Resolution (company name → ticker)
# ──────────────────────────────────────────────

_REVERSE_INDEX: list[tuple[str, str, str]] | None = None
_REVERSE_INDEX_LOCK = asyncio.Lock()


def _normalize_company_name(name: str) -> str:
    s = name.lower().strip()
    for _suffix in [
        " inc", " inc.", " incorporated", " corp", " corp.", " corporation",
        " ltd", " ltd.", " limited", " llc", " lp", " plc",
        " company", " co.", " co", " holdings", " holding",
        " technologies", " technology", " group",
        " (the)", " (new)", " (de)", " (md)", " (mn)",
    ]:
        if s.endswith(_suffix):
            s = s[: -len(_suffix)].strip()
    s = __import__("re").sub(r"[^a-z0-9 ]", "", s)
    s = __import__("re").sub(r"\s+", " ", s).strip()
    return s


async def _build_reverse_index() -> list[tuple[str, str, str]]:
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
        idx.sort(key=lambda x: -len(x[0]))
        _REVERSE_INDEX = idx
        logger.info("Built reverse index with %d entries", len(idx))
        return _REVERSE_INDEX


async def _ticker_sec_lookup(query: str) -> dict | None:
    norm_query = _normalize_company_name(query)
    if not norm_query:
        return None
    idx = await _build_reverse_index()
    query_words = set(norm_query.split())

    best: tuple[int, str, str, str] | None = None
    for norm_title, ticker, title in idx:
        if norm_title == norm_query:
            return {"ticker": ticker, "company_name": title, "source": "sec", "match": "exact"}
        if norm_title.startswith(norm_query):
            score = len(norm_title)
            if best is None or score < best[0]:
                best = (score, ticker, title, "prefix")
        if not query_words:
            continue
        title_words = set(norm_title.split())
        overlap = query_words & title_words
        if overlap and overlap == query_words:
            score = len(norm_title)
            if best is None or score < best[0]:
                best = (score, ticker, title, "word_overlap")

    if best:
        return {"ticker": best[1], "company_name": best[2], "source": "sec", "match": best[3]}

    for word in sorted(query_words, key=len, reverse=True):
        for norm_title, ticker, title in idx:
            if norm_title.startswith(word):
                return {"ticker": ticker, "company_name": title, "source": "sec", "match": f"word_prefix:{word}"}
            if word in norm_title.split():
                return {"ticker": ticker, "company_name": title, "source": "sec", "match": f"word_in:{word}"}
    return None


_STOCK_TICKER_RE = __import__("re").compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")


def _is_plausible_ticker(symbol: str) -> bool:
    return bool(_STOCK_TICKER_RE.match(symbol))


async def _is_sec_ticker(symbol: str) -> bool:
    await _prewarm_ticker_map()
    return symbol.upper() in (_edgar._title_map or {})


async def _ticker_yahoo_search(query: str) -> dict | None:
    try:
        c = await _edgar._get_client()
        url = f"https://query1.finance.yahoo.com/v1/finance/search?q={urlquote(query)}&lang=en-US&region=US&quotesCount=5"
        resp = await c.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        resp.raise_for_status()
        data = resp.json()
        quotes = data.get("quotes", [])
        for q in quotes:
            symbol = q.get("symbol", "")
            if not symbol or not _is_plausible_ticker(symbol):
                continue
            exch = q.get("exchange", "")
            if exch in ("NYQ", "NMS", "NGM", "ASE", "NYQ", "PCX", "OQA", "OQB"):
                return {
                    "ticker": symbol,
                    "company_name": q.get("longname", q.get("shortname", q.get("symbol", ""))),
                    "source": "yfinance",
                }
        for q in quotes:
            symbol = q.get("symbol", "")
            if not symbol or not _is_plausible_ticker(symbol):
                continue
            if await _is_sec_ticker(symbol):
                return {
                    "ticker": symbol,
                    "company_name": q.get("longname", q.get("shortname", q.get("symbol", ""))),
                    "source": "yfinance",
                }
        for q in quotes:
            symbol = q.get("symbol", "")
            if not symbol or not _is_plausible_ticker(symbol):
                continue
            return {
                "ticker": symbol,
                "company_name": q.get("longname", q.get("shortname", q.get("symbol", ""))),
                "source": "yfinance",
            }
    except Exception as exc:
        logger.warning("Yahoo Finance search failed for %s: %s", query, exc)
    return None


@app.tool()
async def resolve_company_ticker(text: str) -> dict:
    """Resolve a company name or natural language text to a stock ticker symbol.

    Tries SEC EDGAR reverse lookup first (instant, local cache), then falls back
    to Yahoo Finance search API.

    Args:
        text: Company name or query text (e.g. \"Mastercard\", \"Apple Inc\", \"Microsoft Corporation\")

    Returns:
        dict with keys: ticker, company_name, source (sec/yfinance), match (exact/prefix/word_overlap), or error
    """
    try:
        result = await _ticker_sec_lookup(text)
        if result:
            return result
        result = await _ticker_yahoo_search(text)
        if result:
            return result
        return {"error": f"Could not resolve '{text}' to a stock ticker"}
    except Exception as exc:
        return {"error": f"Ticker resolution failed: {exc}"}


# ──────────────────────────────────────────────
# Financial News Tools
# ──────────────────────────────────────────────

_sentiment_analyzer = SentimentIntensityAnalyzer()

_RSS_FEEDS: dict[str, str] = {
    "yahoo_finance": "https://finance.yahoo.com/news/rssindex",
    "cnbc_top": (
        "https://search.cnbc.com/rs/search/combinedcms/view.xml"
        "?partnerId=wrss01&id=100003114"
    ),
    "marketwatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
}


async def _fetch_rss(url: str, client: httpx.AsyncClient) -> feedparser.FeedParserDict:
    """Fetch RSS content asynchronously, then parse with feedparser."""
    try:
        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        return feedparser.parse(resp.text)
    except Exception as exc:
        logger.warning("RSS fetch failed for %s: %s", url, exc)
        return feedparser.FeedParserDict(entries=[])


async def _resolve_company_keywords(ticker: str) -> list[str]:
    """Build a robust keyword list for RSS headline matching.

    For JPM ("JPMORGAN CHASE & CO") this produces:
      ["jpm", "jpmorgan", "chase", "jpmorgan chase", "jp morgan"]
    which matches "JPMorgan reports...", "JP Morgan raises...", "J.P. Morgan...",
    "JPM stock...", after headlines are normalised by _normalise_for_match().
    """
    ticker_upper = ticker.upper()
    keywords: list[str] = [ticker.lower()]

    try:
        title = await _edgar.get_company_title(ticker_upper)
    except Exception:
        title = ""

    if title:
        words = [w.lower() for w in title.split() if len(w) > 2]
        keywords.extend(words)
        for j in range(len(words) - 1):
            keywords.append(f"{words[j]} {words[j + 1]}")
        for w in words:
            if len(w) >= 5:
                keywords.append(f"{w[:2]} {w[2:]}")

    seen: set[str] = set()
    unique: list[str] = []
    for k in keywords:
        if k and k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


def _normalise_for_match(text: str) -> str:
    """Normalise a headline/summary for keyword matching.

    Strips punctuation and collapses single-letter tokens produced by
    removing dots from abbreviations:
      "J.P. Morgan" -> "jpmorgan"  (matches keyword "jpmorgan")
      "JP Morgan"   -> "jp morgan" (matches keyword "jp morgan")
    """
    s = __import__("re").sub(r"[^a-z0-9 ]", " ", text.lower())
    s = __import__("re").sub(r"\b([a-z])\b\s*", r"\1", s)
    return __import__("re").sub(r"  +", " ", s).strip()


@app.tool()
async def get_news_sentiment(ticker: str, limit: int = 10) -> dict:
    """Fetch recent financial news mentioning a ticker and compute VADER sentiment.

    Sources: Yahoo Finance, CNBC, MarketWatch RSS feeds.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        limit: Maximum articles to return (default 10)

    Returns:
        dict with keys: ticker, total_articles, sentiment_score (-1 to 1),
        positive_articles, negative_articles, neutral_articles, articles
    """
    keywords = await _resolve_company_keywords(ticker)

    async with httpx.AsyncClient(headers=_SEC_HEADERS) as client:
        feed_results = await asyncio.gather(
            *[_fetch_rss(url, client) for url in _RSS_FEEDS.values()],
            return_exceptions=True,
        )

    articles: list[dict] = []
    scores: list[float] = []

    for source, feed in zip(_RSS_FEEDS.keys(), feed_results):
        if isinstance(feed, Exception):
            logger.warning("Feed %s raised: %s", source, feed)
            continue
        for entry in feed.entries[:15]:
            title: str = entry.get("title", "")
            summary: str = entry.get("summary", "")
            combined = _normalise_for_match(f"{title} {summary}")
            if not any(k in combined for k in keywords):
                continue

            title_score = _sentiment_analyzer.polarity_scores(title)["compound"]
            summary_score = _sentiment_analyzer.polarity_scores(summary)["compound"] if summary else title_score
            compound = round((title_score + summary_score) / 2, 4)

            scores.append(compound)
            articles.append({
                "source": source,
                "title": title,
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "sentiment": compound,
            })

    avg = round(sum(scores) / len(scores), 4) if scores else 0.0
    pos = sum(1 for s in scores if s > 0.05)
    neg = sum(1 for s in scores if s < -0.05)
    return {
        "ticker": ticker.upper(),
        "total_articles": len(articles),
        "sentiment_score": avg,
        "positive_articles": pos,
        "negative_articles": neg,
        "neutral_articles": len(scores) - pos - neg,
        "articles": articles[:limit],
    }


@app.tool()
async def get_earnings_calendar(ticker: str) -> dict:
    """Fetch upcoming earnings date for a stock ticker via yfinance.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)

    Returns:
        dict with keys: ticker, next_earnings_date, source, or error
    """
    try:
        stock = yf.Ticker(ticker)
        cal = stock.calendar
        if cal and "Earnings Date" in cal:
            raw = cal["Earnings Date"]
            dates = raw if isinstance(raw, (list, tuple)) else [raw]
            iso_dates = [
                d.isoformat() if hasattr(d, "isoformat") else str(d)
                for d in dates
                if d is not None
            ]
            if iso_dates:
                return {
                    "ticker": ticker.upper(),
                    "next_earnings_date": iso_dates[0],
                    "all_dates": iso_dates,
                    "source": "yfinance",
                }
    except Exception as exc:
        logger.warning("yfinance calendar failed for %s: %s", ticker, exc)

    try:
        cik = await _edgar._lookup_cik(ticker)
        c = await _edgar._get_client()
        resp = await c.get(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        )
        resp.raise_for_status()
        facts = resp.json()
        eps_data = (
            facts.get("facts", {})
            .get("us-gaap", {})
            .get("EarningsPerShareBasic", {})
            .get("units", {})
            .get("USD/shares", [])
        )
        if eps_data:
            filed_dates = sorted(
                {e["filed"] for e in eps_data if "filed" in e}, reverse=True
            )
            return {
                "ticker": ticker.upper(),
                "last_eps_filed": filed_dates[0] if filed_dates else None,
                "recent_eps_filings": filed_dates[:4],
                "note": "Exact next earnings date unavailable; showing recent filing cadence.",
                "source": "sec_edgar_xbrl",
            }
    except Exception as exc:
        logger.warning("EDGAR earnings fallback failed for %s: %s", ticker, exc)

    return {"ticker": ticker.upper(), "error": "Could not retrieve earnings date from any source"}


# ──────────────────────────────────────────────
# Hardened Python Sandbox
# ──────────────────────────────────────────────

_RESTRICTED_IMPORTS = frozenset([
    "os", "subprocess", "shutil", "socket", "ctypes",
    "importlib", "pickle", "inspect", "sys", "builtins",
    "gc", "weakref", "atexit", "signal", "threading",
    "multiprocessing", "pty", "tty", "termios", "fcntl",
    "mmap", "resource", "pwd", "grp", "crypt",
])

_RESTRICTED_CALLS = frozenset([
    "exec", "eval", "open", "__import__", "compile",
    "globals", "locals", "vars", "dir", "delattr", "setattr",
])

_RESTRICTED_ATTRS = frozenset([
    "system", "popen", "execv", "execve", "execl", "execvp",
    "spawn", "spawnl", "fork", "forkpty", "exec", "eval",
    "__class__", "__bases__", "__subclasses__", "__mro__",
    "__globals__", "__builtins__", "__code__", "__closure__",
    "__wrapped__",
])


def _check_code_safety(code: str) -> tuple[bool, str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"Syntax error: {exc}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _RESTRICTED_IMPORTS:
                    return False, f"Restricted import: {alias.name}"
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _RESTRICTED_IMPORTS:
                return False, f"Restricted import from: {node.module}"

        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _RESTRICTED_CALLS:
                return False, f"Restricted function: {fn.id}"
            if isinstance(fn, ast.Attribute) and fn.attr in _RESTRICTED_ATTRS:
                return False, f"Restricted attribute: {fn.attr}"

        if isinstance(node, ast.Attribute) and node.attr in _RESTRICTED_ATTRS:
            return False, f"Restricted attribute access: {node.attr}"

        if isinstance(node, ast.Subscript):
            slice_node = node.slice
            if isinstance(slice_node, ast.Constant) and isinstance(
                slice_node.value, str
            ):
                val = slice_node.value
                if val in _RESTRICTED_ATTRS or (
                    val.startswith("__") and val.endswith("__")
                ):
                    return False, f"Restricted subscript key: {val}"

        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "getattr":
                if len(node.args) >= 2:
                    attr_arg = node.args[1]
                    if isinstance(attr_arg, ast.Constant) and isinstance(
                        attr_arg.value, str
                    ) and (
                        attr_arg.value in _RESTRICTED_ATTRS
                        or (attr_arg.value.startswith("__") and attr_arg.value.endswith("__"))
                    ):
                        return False, f"Restricted getattr: {attr_arg.value}"

    return True, ""


def _sandbox_preexec() -> None:
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (0, 0))
    except Exception:
        pass


_SANDBOX_RUNNER = """\
import sys, json, math, statistics, itertools, collections, functools, \
       typing, datetime, random

_SAFE_BUILTINS = {
    "print": print, "len": len, "range": range,
    "int": int, "float": float, "str": str, "bool": bool,
    "list": list, "dict": dict, "tuple": tuple, "set": set,
    "frozenset": frozenset, "bytes": bytes,
    "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,
    "any": any, "all": all, "sum": sum, "min": min, "max": max,
    "abs": abs, "round": round, "sorted": sorted, "reversed": reversed,
    "repr": repr, "format": format, "hash": hash, "id": id,
    "isinstance": isinstance,
    "True": True, "False": False, "None": None,
    "Exception": Exception, "ValueError": ValueError,
    "TypeError": TypeError, "KeyError": KeyError,
    "IndexError": IndexError, "StopIteration": StopIteration,
    "NotImplementedError": NotImplementedError,
}

_GLOBALS = {
    "__builtins__": _SAFE_BUILTINS,
    "pd": __import__("pandas"),
    "np": __import__("numpy"),
    "math": math, "json": json, "datetime": datetime,
    "random": random, "statistics": statistics,
    "itertools": itertools, "collections": collections,
    "functools": functools, "typing": typing,
}

code = sys.stdin.read()
_locals = {}
try:
    exec(code, _GLOBALS, _locals)
    result = _locals.get("result")
    print("__RESULT__:" + json.dumps({
        "type": type(result).__name__ if result is not None else "NoneType",
        "value": repr(result)[:2000],
    }))
except Exception:
    import traceback
    print("__ERROR__:" + traceback.format_exc(), file=sys.stderr)
"""


@app.tool()
async def execute_python(code: str, timeout: int = 30) -> dict:
    """Execute Python code in a hardened sandbox subprocess.

    Available: pandas (pd), numpy (np), math, json, datetime, random,
    statistics, itertools, collections, functools, typing.
    NOT available: os, sys, subprocess, open, exec, eval, and all dunder tricks.
    Assign to `result` to get a value back.

    Resource limits (Unix): 25 s CPU, 512 MB RAM, 0 open file descriptors.

    Args:
        code: Python code to execute.
        timeout: Wall-clock timeout in seconds (default 30).

    Returns:
        dict with keys: success, stdout, stderr, result ({type, value})
    """
    safe, reason = _check_code_safety(code)
    if not safe:
        return {"success": False, "stdout": "", "stderr": reason, "result": None}

    runner_fd, runner_path = tempfile.mkstemp(suffix=".py", text=True)
    try:
        with os.fdopen(runner_fd, "w", encoding="utf-8") as f:
            f.write(_SANDBOX_RUNNER)

        preexec = _sandbox_preexec if sys.platform != "win32" else None

        proc = subprocess.run(
            [sys.executable, "-I", "-S", runner_path],
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=preexec,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False, "stdout": "", "stderr": f"Timed out after {timeout}s",
            "result": None,
        }
    except Exception as exc:
        return {"success": False, "stdout": "", "stderr": str(exc), "result": None}
    finally:
        try:
            os.unlink(runner_path)
        except OSError:
            pass

    result: dict | None = None
    clean_lines: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__:"):
            try:
                result = json.loads(line[len("__RESULT__:"):])
            except json.JSONDecodeError:
                result = {"raw": line[len("__RESULT__:"):]}
        else:
            clean_lines.append(line)

    cleaned_stderr_lines = []
    for line in proc.stderr.splitlines():
        cleaned_stderr_lines.append(
            line[len("__ERROR__:"):] if line.startswith("__ERROR__:") else line
        )
    stderr = "\n".join(cleaned_stderr_lines)

    return {
        "success": proc.returncode == 0,
        "stdout": "\n".join(clean_lines),
        "stderr": stderr,
        "result": result,
    }


# ──────────────────────────────────────────────
# Thread-safe server app singleton
# ──────────────────────────────────────────────

_starlette_app = None
_app_lock = threading.Lock()


def get_app():
    global _starlette_app
    if _starlette_app is None:
        with _app_lock:
            if _starlette_app is None:
                _starlette_app = app.sse_app()
    return _starlette_app


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(get_app(), host=HOST, port=PORT)
