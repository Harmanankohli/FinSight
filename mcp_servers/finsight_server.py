from __future__ import annotations

import asyncio
import ast
import atexit
import json
import logging
import os
import sys
import subprocess
import time

if sys.platform != "win32":
    import resource
import tempfile
import threading
from collections import OrderedDict
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
from langfuse import observe
from mcp.server.fastmcp import FastMCP
from sentence_transformers import SentenceTransformer
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from shared.observability import init_langfuse, shutdown_langfuse
init_langfuse(service_name="mcp_server")
atexit.register(shutdown_langfuse)

from shared.logging_config import setup_file_logging
setup_file_logging("mcp")
logger = logging.getLogger(__name__)
app = FastMCP("finsight-mcp")

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8010"))
AGENT_CARDS_DIR = Path(__file__).resolve().parent.parent / "agent_cards"
EMBED_MODEL_NAME = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")


# ──────────────────────────────────────────────
# TTL Cache
# ──────────────────────────────────────────────

class _TTLCache:
    """Thread-safe in-process TTL cache backed by an OrderedDict LRU eviction."""

    def __init__(self, ttl: float | None, maxsize: int = 200):
        self._ttl = ttl
        self._store: OrderedDict = OrderedDict()
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._store:
                val, ts = self._store[key]
                if self._ttl is None or time.monotonic() - ts < self._ttl:
                    self._store.move_to_end(key)
                    return val
                del self._store[key]
        return None

    def set(self, key, val):
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            elif len(self._store) >= self._maxsize:
                self._store.popitem(last=False)
            self._store[key] = (val, time.monotonic())


_cache_prices      = _TTLCache(ttl=300)        # 5 min
_cache_financials  = _TTLCache(ttl=86400)      # 24 h
_cache_news        = _TTLCache(ttl=900)        # 15 min
_cache_filing      = _TTLCache(ttl=None, maxsize=200)  # permanent LRU-200
_cache_submissions = _TTLCache(ttl=21600)      # 6 h


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
@observe()
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
    """Recursively convert non-JSON-serialisable types."""
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
@observe()
async def get_prices(ticker: str, period: str = "1y", interval: str = "1d") -> dict:
    """Fetch OHLCV price history data for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        period: Time period. Options: 1d 5d 1mo 3mo 6mo 1y 2y 5y 10y ytd max
        interval: Data interval. Options: 1m 2m 5m 15m 30m 60m 90m 1h 1d 5d 1wk 1mo 3mo

    Returns:
        dict with keys: ticker, period, data (list of OHLCV records with ISO dates)
    """
    cache_key = (ticker.upper(), period, interval)
    cached = _cache_prices.get(cache_key)
    if cached is not None:
        logger.debug("Cache hit: get_prices(%s, %s, %s)", ticker, period, interval)
        return cached
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period, interval=interval)
        records = _serialise_value(hist.reset_index().to_dict(orient="records"))
        result = {"ticker": ticker, "period": period, "data": records}
    except Exception as exc:
        logger.warning("get_prices failed for %s: %s", ticker, exc)
        result = {"ticker": ticker, "period": period, "error": str(exc), "data": []}
    _cache_prices.set(cache_key, result)
    return result


@app.tool()
@observe()
async def get_financials(ticker: str) -> dict:
    """Fetch financial statements and company info for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)

    Returns:
        dict with keys: income_statement, balance_sheet, cash_flow, info
    """
    cache_key = (ticker.upper(),)
    cached = _cache_financials.get(cache_key)
    if cached is not None:
        logger.debug("Cache hit: get_financials(%s)", ticker)
        return cached
    try:
        stock = yf.Ticker(ticker)
        result = _serialise_value({
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
        result = {
            "ticker": ticker, "error": str(exc),
            "income_statement": {}, "balance_sheet": {}, "cash_flow": {}, "info": {},
        }
    _cache_financials.set(cache_key, result)
    return result


@app.tool()
@observe()
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
# SEC EDGAR Client
# ──────────────────────────────────────────────

_SEC_HEADERS = {
    "User-Agent": "FinSight Research (contact@finsight.com)",
    "Accept-Encoding": "gzip, deflate",
}

# Form types that contain structured financial statements.
# Used as the default filter for get_financial_filings and RAG ingest guidance.
FINANCIAL_FORM_TYPES: frozenset[str] = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})

# Form types whose primaryDocument is an XSLT stylesheet path rather than
# an HTML filing. For these, use the accession-number index URL instead.
_INDEX_ONLY_FORMS: frozenset[str] = frozenset({"3", "4", "5", "3/A", "4/A", "5/A"})


class _EdgarClient:
    """Async SEC EDGAR client with shared httpx session, cached ticker/title
    maps, URL-safe edgar_url construction, and parallel-array bounds checking."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._cik_cache: dict[str, str] = {}
        self._ticker_map: dict[str, str] | None = None      # {TICKER: cik_zfill10}
        self._title_map: dict[str, str] | None = None       # {TICKER: company title}
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
        """Download {TICKER: cik} and {TICKER: title} maps once, then cache."""
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
        """Return the SEC-registered company name for a ticker (cached)."""
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

    def _build_filing_urls(
        self, cik_short: str, form: str, doc: str, acc_num: str
    ) -> tuple[str, str]:
        """Return (edgar_url, ix_url) for a filing entry.

        edgar_url  — direct link to the raw filing document (or index for Form 3/4/5).
        ix_url     — inline XBRL viewer URL (useful for structured XBRL filings).
        """
        acc_clean = acc_num.replace("-", "")
        safe_doc = urlquote(doc, safe="")

        if form in _INDEX_ONLY_FORMS or "/" in doc:
            # Form 4 etc. store an XSLT path in primaryDocument — use index page.
            edgar_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_short}/{acc_clean}/{acc_num}-index.htm"
            )
        else:
            edgar_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_short}/{acc_clean}/{safe_doc}"
            )

        ix_url = (
            f"https://www.sec.gov/ix?doc=/Archives/edgar/data/"
            f"{cik_short}/{acc_clean}/{safe_doc}"
        )
        return edgar_url, ix_url

    async def _fetch_submissions(self, cik: str, ticker: str) -> dict:
        """Fetch the submissions JSON for a CIK with 3-attempt retry."""
        cached = _cache_submissions.get(cik)
        if cached is not None:
            logger.debug("Cache hit: _fetch_submissions(%s)", cik)
            return cached
        c = await self._get_client()
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        for attempt in range(3):
            try:
                resp = await c.get(url)
                resp.raise_for_status()
                result = resp.json()
                _cache_submissions.set(cik, result)
                return result
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise RuntimeError(
                        f"get_company_filings failed for {ticker} after 3 retries"
                    )
        return {}

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

        try:
            data = await self._fetch_submissions(cik, ticker)
        except Exception as exc:
            return {"ticker": ticker, "error": str(exc), "filings": []}

        cik_short = str(int(cik))
        recent = data.get("filings", {}).get("recent", {})
        forms    = recent.get("form", [])
        dates    = recent.get("filingDate", [])
        docs     = recent.get("primaryDocument", [])
        acc_nums = recent.get("accessionNumber", [])
        n = min(len(forms), len(dates), len(docs), len(acc_nums))

        result = []
        for i in range(n):
            if form_types and forms[i] not in form_types:
                continue
            if len(result) >= limit:
                break
            edgar_url, ix_url = self._build_filing_urls(
                cik_short, forms[i], docs[i], acc_nums[i]
            )
            result.append({
                "form": forms[i],
                "filing_date": dates[i],
                "description": docs[i],
                "edgar_url": edgar_url,
                "ix_url": ix_url,
            })
        return {"ticker": ticker, "cik": cik, "filings": result}

    async def get_financial_filings(
        self,
        ticker: str,
        annual_limit: int = 5,
        quarterly_limit: int = 8,
    ) -> dict:
        """Fetch only financial statement filings (10-K and 10-Q) for a ticker.

        Fetches annual (10-K) and quarterly (10-Q) filings separately to
        guarantee a balanced result — avoids the common failure mode where
        the default get_company_filings returns mostly 8-Ks.

        Also checks the filing's older filings pages if the recent batch
        doesn't have enough 10-Ks (large companies file many other forms).

        Args:
            ticker:          Stock ticker symbol.
            annual_limit:    Max 10-K filings to return (default 5 = ~5 years).
            quarterly_limit: Max 10-Q filings to return (default 8 = ~2 years of quarters).

        Returns:
            dict with keys: ticker, cik, annual (list), quarterly (list),
            total_filings, note
        """
        try:
            cik = await self._lookup_cik(ticker)
        except Exception as exc:
            return {"ticker": ticker, "error": str(exc), "annual": [], "quarterly": []}

        try:
            data = await self._fetch_submissions(cik, ticker)
        except Exception as exc:
            return {"ticker": ticker, "error": str(exc), "annual": [], "quarterly": []}

        cik_short = str(int(cik))
        annual: list[dict] = []
        quarterly: list[dict] = []

        def _process_recent(recent: dict) -> None:
            forms    = recent.get("form", [])
            dates    = recent.get("filingDate", [])
            docs     = recent.get("primaryDocument", [])
            acc_nums = recent.get("accessionNumber", [])
            n = min(len(forms), len(dates), len(docs), len(acc_nums))
            for i in range(n):
                form = forms[i]
                if form not in ("10-K", "10-K/A", "10-Q", "10-Q/A"):
                    continue
                edgar_url, ix_url = self._build_filing_urls(
                    cik_short, form, docs[i], acc_nums[i]
                )
                entry = {
                    "form": form,
                    "filing_date": dates[i],
                    "description": docs[i],
                    "edgar_url": edgar_url,
                    "ix_url": ix_url,
                }
                if form in ("10-K", "10-K/A") and len(annual) < annual_limit:
                    annual.append(entry)
                elif form in ("10-Q", "10-Q/A") and len(quarterly) < quarterly_limit:
                    quarterly.append(entry)

        # Process the primary recent batch
        _process_recent(data.get("filings", {}).get("recent", {}))

        # If we still need more filings, page through older filing batches.
        # EDGAR stores older filings in separate paginated JSON files.
        files_meta = data.get("filings", {}).get("files", [])
        for file_meta in files_meta:
            if len(annual) >= annual_limit and len(quarterly) >= quarterly_limit:
                break
            fname = file_meta.get("name", "")
            if not fname:
                continue
            try:
                c = await self._get_client()
                resp = await c.get(
                    f"https://data.sec.gov/submissions/{fname}"
                )
                resp.raise_for_status()
                older = resp.json()
                _process_recent(older)
            except Exception as exc:
                logger.warning("Failed to fetch older filings page %s: %s", fname, exc)

        note_parts = []
        if len(annual) < annual_limit:
            note_parts.append(
                f"Only {len(annual)} annual filing(s) found (requested {annual_limit})."
            )
        if len(quarterly) < quarterly_limit:
            note_parts.append(
                f"Only {len(quarterly)} quarterly filing(s) found (requested {quarterly_limit})."
            )

        return {
            "ticker": ticker.upper(),
            "cik": cik,
            "annual": annual,
            "quarterly": quarterly,
            "total_filings": len(annual) + len(quarterly),
            "note": " ".join(note_parts) if note_parts else (
                f"Retrieved {len(annual)} annual and {len(quarterly)} quarterly filings."
            ),
        }

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

    async def get_filing_content(
        self, edgar_url: str, ix_url: str | None = None
    ) -> dict:
        """Fetch and extract text content from an SEC EDGAR filing URL."""
        tried_urls = [edgar_url]
        if ix_url and ix_url != edgar_url:
            tried_urls.append(ix_url)

        for url in tried_urls:
            try:
                c = await self._get_client()
                resp = await c.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "")

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
                    for tag in soup(["script", "style", "noscript"]):
                        tag.decompose()
                    text = soup.get_text(separator=" ", strip=True)

                text = " ".join(text.split())
                if len(text) > 50000:
                    text = text[:50000] + "..."
                if len(text.strip()) < 100:
                    continue
                return {"url": url, "content": text, "length": len(text)}
            except Exception as exc:
                logger.info("Tried %s, got: %s", url, exc)
                continue

        return {
            "url": edgar_url,
            "error": "Could not extract content from any URL",
            "content": "",
        }


_edgar = _EdgarClient()


# ──────────────────────────────────────────────
# SEC EDGAR MCP Tools
# ──────────────────────────────────────────────

@app.tool()
@observe()
async def get_company_filings(
    ticker: str, form_types: str = "", limit: int = 10
) -> dict:
    """Retrieve SEC filings for a publicly traded company.

    For fundamental financial analysis, prefer get_financial_filings() which
    automatically fetches only 10-K/10-Q filings in the right quantities.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        form_types: Comma-separated form types to filter (e.g. "10-K,10-Q,8-K"). Empty = all.
        limit: Maximum number of filings to return (default 10)

    Returns:
        dict with keys: ticker, cik, filings (list of {form, filing_date, description, edgar_url, ix_url})
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
@observe()
async def get_financial_filings(
    ticker: str,
    annual_limit: int = 5,
    quarterly_limit: int = 8,
) -> dict:
    """Retrieve financial statement filings (10-K and 10-Q only) for a ticker.

    Use this instead of get_company_filings() for any fundamental analysis,
    ratio calculation, trend analysis, or peer comparison task.

    Unlike get_company_filings(), this tool:
    - Filters to ONLY 10-K (annual) and 10-Q (quarterly) — never 8-Ks or Form 4s
    - Paginates through older EDGAR filing batches to reach the requested depth
    - Returns annual and quarterly filings in separate lists for clarity
    - Provides enough history for multi-year trend analysis by default

    Workflow for fundamental analysis:
      1. Call get_financial_filings(ticker) to get 10-K/10-Q URLs
      2. Call get_filing_content(edgar_url) on each to extract financial text
      3. Call get_financials(ticker) for structured yfinance data (ratios, metrics)
      4. Use execute_python() to compute derived metrics (NIM, ROE, CET1, etc.)

    For peer comparison, call get_financial_filings() for each peer ticker.

    Args:
        ticker:          Stock ticker symbol (e.g. JPM, BAC, GS)
        annual_limit:    Number of annual 10-K filings (default 5 ≈ 5 years of history)
        quarterly_limit: Number of quarterly 10-Q filings (default 8 ≈ 2 years of quarters)

    Returns:
        dict with keys:
          ticker, cik,
          annual   (list of {form, filing_date, description, edgar_url, ix_url}),
          quarterly (list of {form, filing_date, description, edgar_url, ix_url}),
          total_filings, note
    """
    try:
        return await _edgar.get_financial_filings(ticker, annual_limit, quarterly_limit)
    except Exception as exc:
        logger.warning("get_financial_filings tool failed for %s: %s", ticker, exc)
        return {
            "ticker": ticker, "error": str(exc),
            "annual": [], "quarterly": [], "total_filings": 0,
        }


@app.tool()
@observe()
async def full_text_search(query: str, ticker: str | None = None) -> dict:
    """Search full text of SEC EDGAR filings.

    Args:
        query: Keywords (e.g. "net interest margin loan loss provision")
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
@observe()
async def get_filing_content(edgar_url: str, ix_url: str | None = None) -> dict:
    """Fetch and extract text content from an SEC EDGAR filing URL.

    Use this after get_financial_filings() or get_company_filings() to read
    the actual text of a 10-K, 10-Q, or other SEC filing.

    Args:
        edgar_url: The EDGAR filing URL (from the 'edgar_url' field in filing results)
        ix_url: Optional inline XBRL viewer URL ('ix_url' field) as fallback

    Returns:
        dict with keys: url, content (extracted text up to 50k chars), length, or error
    """
    cached = _cache_filing.get(edgar_url)
    if cached is not None:
        logger.debug("Cache hit: get_filing_content(%s)", edgar_url[:80])
        return cached
    try:
        result = await _edgar.get_filing_content(edgar_url, ix_url)
    except Exception as exc:
        logger.warning("get_filing_content tool failed: %s", exc)
        result = {"url": edgar_url, "error": str(exc), "content": ""}
    if result.get("content"):
        _cache_filing.set(edgar_url, result)
    return result


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


# ──────────────────────────────────────────────
# Ticker Resolution (company name → ticker)
# ──────────────────────────────────────────────

_REVERSE_INDEX: list[tuple[str, str, str]] | None = None
_REVERSE_INDEX_LOCK = asyncio.Lock()


def _normalize_company_name(name: str) -> str:
    import re as _re
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
    s = _re.sub(r"[^a-z0-9 ]", "", s)
    s = _re.sub(r"\s+", " ", s).strip()
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
            return {
                "ticker": ticker,
                "company_name": title,
                "source": "sec",
                "match": "exact",
            }
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
        return {
            "ticker": best[1],
            "company_name": best[2],
            "source": "sec",
            "match": best[3],
        }

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


import re as _re_module
_STOCK_TICKER_RE = _re_module.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")


def _is_plausible_ticker(symbol: str) -> bool:
    return bool(_STOCK_TICKER_RE.match(symbol))


async def _is_sec_ticker(symbol: str) -> bool:
    await _prewarm_ticker_map()
    return symbol.upper() in (_edgar._title_map or {})


async def _ticker_yahoo_search(query: str) -> dict | None:
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

        # Prefer quotes from major US exchanges
        for q in quotes:
            symbol = q.get("symbol", "")
            if not symbol or not _is_plausible_ticker(symbol):
                continue
            if q.get("exchange", "") in (
                "NYQ", "NMS", "NGM", "ASE", "PCX", "OQA", "OQB"
            ):
                return {
                    "ticker": symbol,
                    "company_name": q.get(
                        "longname", q.get("shortname", symbol)
                    ),
                    "source": "yfinance",
                }

        # Then any SEC-registered ticker
        for q in quotes:
            symbol = q.get("symbol", "")
            if not symbol or not _is_plausible_ticker(symbol):
                continue
            if await _is_sec_ticker(symbol):
                return {
                    "ticker": symbol,
                    "company_name": q.get(
                        "longname", q.get("shortname", symbol)
                    ),
                    "source": "yfinance",
                }

        # Last resort: first plausible ticker
        for q in quotes:
            symbol = q.get("symbol", "")
            if symbol and _is_plausible_ticker(symbol):
                return {
                    "ticker": symbol,
                    "company_name": q.get(
                        "longname", q.get("shortname", symbol)
                    ),
                    "source": "yfinance",
                }

    except Exception as exc:
        logger.warning("Yahoo Finance search failed for %s: %s", query, exc)
    return None


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


async def _fetch_rss(url: str, client: httpx.AsyncClient) -> dict:
    """Async RSS fetch — no blocking I/O in feedparser.

    Returns a dict with:
      entries (list): parsed feed entries, empty on failure
      status  (str):  "ok" | "http_{code}" | "error"
      error   (str):  error message if status != "ok", else ""
    """
    try:
        resp = await client.get(url, timeout=10.0)
        if resp.status_code != 200:
            logger.warning("RSS %s returned HTTP %s", url, resp.status_code)
            return {
                "entries": [],
                "status": f"http_{resp.status_code}",
                "error": f"HTTP {resp.status_code}",
            }
        feed = feedparser.parse(resp.text)
        if feed.bozo and not feed.entries:
            return {
                "entries": [],
                "status": "parse_error",
                "error": str(getattr(feed, "bozo_exception", "unknown parse error")),
            }
        return {"entries": feed.entries, "status": "ok", "error": ""}
    except Exception as exc:
        logger.warning("RSS fetch failed for %s: %s", url, exc)
        return {"entries": [], "status": "error", "error": str(exc)}


async def _fetch_yf_news(ticker: str, client: httpx.AsyncClient, limit: int = 15) -> list[dict]:
    """Fetch news articles from Yahoo Finance search API for a ticker.

    This is a structured fallback when RSS feeds fail or return 0 articles.
    Unlike RSS, results are pre-filtered to the ticker — no keyword matching needed.

    Returns a list of article dicts: {title, link, publisher, published, summary}
    """
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _SA
    _sa = _SA()
    try:
        url = (
            f"https://query1.finance.yahoo.com/v1/finance/search"
            f"?q={urlquote(ticker)}&lang=en-US&region=US&newsCount={limit}&quotesCount=0"
        )
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.warning("YF news API returned HTTP %s for %s", resp.status_code, ticker)
            return []
        data = resp.json()
        articles = []
        for item in data.get("news", [])[:limit]:
            title = item.get("title", "")
            summary = item.get("summary", "") or ""
            pub = item.get("providerPublishTime", 0)
            try:
                published = datetime.fromtimestamp(pub, tz=timezone.utc).isoformat() if pub else ""
            except Exception:
                published = ""
            title_score   = _sa.polarity_scores(title)["compound"]
            summary_score = _sa.polarity_scores(summary)["compound"] if summary else title_score
            compound = round((title_score + summary_score) / 2, 4)
            articles.append({
                "source": "yahoo_finance_api",
                "title": title,
                "link": item.get("link", ""),
                "publisher": item.get("publisher", ""),
                "published": published,
                "sentiment": compound,
            })
        return articles
    except Exception as exc:
        logger.warning("YF news API failed for %s: %s", ticker, exc)
        return []


async def _resolve_company_keywords(ticker: str) -> list[str]:
    """Build a robust keyword list for RSS headline matching.

    For JPM ("JPMORGAN CHASE & CO") produces:
      ["jpm", "jpmorgan", "chase", "jpmorgan chase", "jp morgan"]
    Covers "JPMorgan", "JP Morgan", "J.P. Morgan" after normalisation.
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
        # Bigrams: "jpmorgan" + "chase" -> "jpmorgan chase"
        for j in range(len(words) - 1):
            keywords.append(f"{words[j]} {words[j + 1]}")
        # Split-at-2 variants: "jpmorgan" -> "jp morgan"
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
    """Normalise a headline for keyword matching.

    Strips punctuation and collapses single-char tokens so that
    "J.P. Morgan" -> "jpmorgan" (matches keyword "jpmorgan").
    """
    import re as _re
    s = _re.sub(r"[^a-z0-9 ]", " ", text.lower())
    s = _re.sub(r"\b([a-z])\b\s*", r"\1", s)
    return _re.sub(r"  +", " ", s).strip()


def _keyword_matches(norm_text: str, keywords: list[str]) -> bool:
    """Word-boundary keyword match against a normalised headline.

    Uses \b word boundaries for single-word keywords to avoid substring
    false positives (e.g. "chasing" matching keyword "chase").
    Multi-word keywords (bigrams) use substring match — they are already
    specific enough that a boundary check isn't needed.
    """
    import re as _re
    for kw in keywords:
        if " " in kw:
            if kw in norm_text:
                return True
        else:
            if _re.search(rf"\b{_re.escape(kw)}\b", norm_text):
                return True
    return False


@app.tool()
@observe()
async def get_news_sentiment(ticker: str, limit: int = 10) -> dict:
    """Fetch recent financial news mentioning a ticker and compute VADER sentiment.

    Sources (tried in order):
      1. RSS feeds: Yahoo Finance, CNBC, MarketWatch (concurrent)
      2. Yahoo Finance news search API (fallback if RSS yields 0 articles)

    The response always includes a feed_status field so the agent can tell the
    difference between "no news exists" and "feeds were unreachable".

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT, JPM)
        limit:  Maximum articles to return (default 10)

    Returns:
        dict with keys:
          ticker, total_articles, sentiment_score (-1 to 1),
          positive_articles, negative_articles, neutral_articles,
          articles, feed_status (per-source diagnostics), source_used
    """
    cache_key = (ticker.upper(), limit)
    cached = _cache_news.get(cache_key)
    if cached is not None:
        logger.debug("Cache hit: get_news_sentiment(%s)", ticker)
        return cached
    keywords = await _resolve_company_keywords(ticker)
    articles: list[dict] = []
    scores: list[float] = []
    feed_status: dict[str, str] = {}

    # ── Primary: RSS feeds (concurrent) ─────────────────────────────────────
    async with httpx.AsyncClient(headers=_SEC_HEADERS, follow_redirects=True) as client:
        rss_results = await asyncio.gather(
            *[_fetch_rss(url, client) for url in _RSS_FEEDS.values()],
            return_exceptions=True,
        )

        for source, result in zip(_RSS_FEEDS.keys(), rss_results):
            if isinstance(result, Exception):
                feed_status[source] = f"error: {result}"
                continue

            feed_status[source] = result["status"]
            if result["error"]:
                feed_status[source] += f" ({result['error']})"

            for entry in result["entries"][:15]:
                title: str = entry.get("title", "")
                summary: str = entry.get("summary", "")
                combined = _normalise_for_match(f"{title} {summary}")
                if not _keyword_matches(combined, keywords):
                    continue

                title_score   = _sentiment_analyzer.polarity_scores(title)["compound"]
                summary_score = (
                    _sentiment_analyzer.polarity_scores(summary)["compound"]
                    if summary else title_score
                )
                compound = round((title_score + summary_score) / 2, 4)
                scores.append(compound)
                articles.append({
                    "source": source,
                    "title": title,
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "sentiment": compound,
                })

        source_used = "rss"

        # ── Fallback: Yahoo Finance news API ────────────────────────────────
        # Triggered when ALL RSS feeds failed OR total matched articles is 0.
        rss_ok = any(v == "ok" for v in feed_status.values())
        if not articles:
            reason = "rss_unreachable" if not rss_ok else "rss_no_match"
            logger.info(
                "RSS returned 0 articles for %s (%s), trying YF news API", ticker, reason
            )
            yf_articles = await _fetch_yf_news(ticker, client, limit=limit * 2)
            if yf_articles:
                articles = yf_articles[:limit]
                scores   = [a["sentiment"] for a in articles]
                feed_status["yahoo_finance_api"] = f"ok ({len(yf_articles)} articles)"
                source_used = f"yahoo_finance_api ({reason})"
            else:
                feed_status["yahoo_finance_api"] = "no articles returned"
                source_used = "none"

    avg = round(sum(scores) / len(scores), 4) if scores else 0.0
    pos = sum(1 for s in scores if s > 0.05)
    neg = sum(1 for s in scores if s < -0.05)

    result = {
        "ticker": ticker.upper(),
        "total_articles": len(articles),
        "sentiment_score": avg,
        "positive_articles": pos,
        "negative_articles": neg,
        "neutral_articles": len(scores) - pos - neg,
        "articles": articles[:limit],
        "feed_status": feed_status,
        "source_used": source_used,
    }

    # Surface a clear warning when no articles were found at all, so the
    # agent does not invent narrative to fill the gap.
    if not articles:
        feeds_ok   = [k for k, v in feed_status.items() if "ok" in v]
        feeds_fail = [k for k, v in feed_status.items() if "ok" not in v]
        if feeds_fail and not feeds_ok:
            result["warning"] = (
                "All news sources were unreachable. Do not invent sentiment narrative. "
                f"Failed sources: {feeds_fail}. Retry or report as data unavailable."
            )
        else:
            result["warning"] = (
                "No news articles found for this ticker in current RSS feeds. "
                "This may indicate low media coverage, not negative sentiment. "
                "Do not infer sentiment from absence of data."
            )
    if articles:
        _cache_news.set(cache_key, result)
    return result


@app.tool()
@observe()
async def get_earnings_calendar(ticker: str) -> dict:
    """Fetch upcoming earnings date for a stock ticker.

    Tries yfinance first, falls back to SEC EDGAR XBRL filing cadence.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT, JPM)

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

    return {
        "ticker": ticker.upper(),
        "error": "Could not retrieve earnings date from any source",
    }


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
            # Block getattr(obj, '__dangerous__') with string literal
            if isinstance(fn, ast.Name) and fn.id == "getattr":
                if len(node.args) >= 2:
                    attr_arg = node.args[1]
                    if isinstance(attr_arg, ast.Constant) and isinstance(
                        attr_arg.value, str
                    ) and (
                        attr_arg.value in _RESTRICTED_ATTRS
                        or (
                            attr_arg.value.startswith("__")
                            and attr_arg.value.endswith("__")
                        )
                    ):
                        return False, f"Restricted getattr: {attr_arg.value}"

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

    return True, ""


def _sandbox_preexec() -> None:
    """OS-level resource limits applied in the child process (Unix only)."""
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
@observe()
async def execute_python(code: str, timeout: int = 30) -> dict:
    """Execute Python code in a hardened sandbox subprocess.

    Available libraries: pandas (pd), numpy (np), math, json, datetime,
    random, statistics, itertools, collections, functools, typing.
    NOT available: os, sys, subprocess, open, exec, eval, dunder tricks.
    Set `result = <value>` to return data.

    Resource limits (Unix): 25 s CPU, 512 MB RAM, 0 open file descriptors.

    Args:
        code:    Python code string to execute.
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
            "success": False,
            "stdout": "",
            "stderr": f"Timed out after {timeout}s",
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

    cleaned_stderr = "\n".join(
        line[len("__ERROR__:"):] if line.startswith("__ERROR__:") else line
        for line in proc.stderr.splitlines()
    )

    return {
        "success": proc.returncode == 0,
        "stdout": "\n".join(clean_lines),
        "stderr": cleaned_stderr,
        "result": result,
    }


# ──────────────────────────────────────────────
# Thread-safe server app singleton
# ──────────────────────────────────────────────

_starlette_app = None
_app_lock = threading.Lock()


async def _health(scope, receive, send):
    """Minimal ASGI health endpoint mounted alongside the MCP SSE app."""
    import json as _json
    body = _json.dumps({"status": "ok", "agent": "mcp"}).encode()
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


def get_app():
    global _starlette_app
    if _starlette_app is None:
        with _app_lock:
            if _starlette_app is None:
                from starlette.applications import Starlette
                from starlette.routing import Route, Mount
                from starlette.responses import JSONResponse

                async def health(request):
                    return JSONResponse({"status": "ok", "agent": "mcp"})

                mcp_asgi = app.sse_app()
                _starlette_app = Starlette(routes=[
                    Route("/health", health),
                    Mount("/", app=mcp_asgi),
                ])
    return _starlette_app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(get_app(), host=HOST, port=PORT)