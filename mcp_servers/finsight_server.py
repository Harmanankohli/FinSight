"""Unified MCP data server providing financial data tools.

Exposes stock prices, financials, SEC EDGAR filings, news sentiment, ticker
resolution, and a hardened Python sandbox — all over the Model Context Protocol.

Agents consume these tools through the FastMCP SSE endpoint; no REST layer needed.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import sys
import subprocess
import time
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
from langfuse import observe
from mcp.server.fastmcp import FastMCP
from sentence_transformers import SentenceTransformer
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from shared.config import SEC_USER_AGENT
from shared.rate_limiter import TokenBucket
from shared.redis_cache import make_cache
from shared.logging_config import logged
from shared.observability import init_langfuse, shutdown_langfuse
init_langfuse(service_name="mcp_server")
atexit.register(shutdown_langfuse)

from shared.logging_config import setup_file_logging
setup_file_logging("mcp")
logger = logging.getLogger(__name__)
app = FastMCP("finsight-mcp")

# Bind to all interfaces by default so the containerised MCP SSE endpoint is
# reachable from any agent process — 8010 avoids collisions with common ports.
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8010"))
# Agent cards live in the parent project root, not inside mcp_servers/, to keep
# them editable by non-engineers who may not know the server layout.
AGENT_CARDS_DIR = Path(__file__).resolve().parent.parent / "agent_cards"
# all-MiniLM-L6-v2: 23 MB (vs 80+ MB for larger models), 384-dim, fast CPU
# inference — good enough for agent card semantic search without GPU cost.
EMBED_MODEL_NAME = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")

# ──────────────────────────────────────────────
# Rate limiters
# ──────────────────────────────────────────────
_sec_limiter = TokenBucket(rate=8, burst=10)      # SEC: 10 req/s hard cap; stay below
_yfinance_limiter = TokenBucket(rate=4, burst=8)  # Yahoo Finance: no published cap, conservative
_rss_limiter = TokenBucket(rate=2, burst=4)       # RSS + Yahoo news fallback


# ──────────────────────────────────────────────
# TTL Cache
# ──────────────────────────────────────────────

_cache_prices      = make_cache(60,    "prices")                       # 1 min — intraday prices
_cache_benchmark   = make_cache(3600,  "benchmark")                    # 1 hr — index benchmarks (^GSPC etc.)
_cache_financials  = make_cache(3600,  "financials")                   # 1 hr — quarterly data
_cache_news        = make_cache(300,   "news")                         # 5 min — news headlines
_cache_filing      = make_cache(None,  "filing",   max_entries=200)    # permanent LRU-200
_cache_submissions = make_cache(21600, "submissions")                  # 6 hr — submission lists

_BENCHMARK_TICKERS = frozenset({"^GSPC", "^IXIC", "^DJI", "^RUT", "^VIX", "DXY"})


# ──────────────────────────────────────────────
# Lazy Agent Registry (no model download at import time)
# ──────────────────────────────────────────────

# Lazy initialisation: SentenceTransformer downloads ~80 MB on first call.
# We defer everything until the first tool invocation so import time stays fast
# and servers that never call find_agent don't pay the model download cost.
_registry_lock = asyncio.Lock()
_registry_ready = False
_model_embed: SentenceTransformer | None = None
_df_registry: pd.DataFrame = pd.DataFrame(
    columns=["card_uri", "agent_card", "card_embeddings"]
)


def _load_agent_cards() -> tuple[list[str], list[dict]]:
    """Load agent card JSON files from AGENT_CARDS_DIR synchronously (called once).
    
    Runs inside run_in_executor so the GIL doesn't block the event loop during
    file I/O, even though the work is trivially fast for typical card counts.
    """
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
    # Fast path: avoid lock acquisition when already initialised.
    if _registry_ready:
        return
    # Double-checked locking: only one coroutine downloads the model.
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
            # SentenceTransformer downloads ~80 MB on first call — delay until needed.
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
    """Semantic search over agent cards: embed query, return highest cosine-similarity card."""
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
    """List all available agent card URIs as a JSON resource."""
    await _ensure_registry()
    return {
        "agent_cards": _df_registry["card_uri"].to_list()
        if not _df_registry.empty
        else []
    }


@app.resource("resource://agent_cards/{card_name}", mime_type="application/json")
async def get_agent_card(card_name: str) -> dict:
    """Retrieve a single agent card by name (e.g. resource://agent_cards/analyst)."""
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
    """Recursively convert non-JSON-serialisable types (NaN, inf, datetime, numpy)."""
    if isinstance(v, dict):
        return {_serialise_value(k): _serialise_value(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_serialise_value(i) for i in v]
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None  # NaN and ±Inf are not valid JSON — replace with null.
    if hasattr(v, "isoformat"):
        return v.isoformat()  # datetime / Timestamp → ISO-8601 string.
    if isinstance(v, (np.integer,)):
        return int(v)  # numpy int64/32/... → native Python int.
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, np.ndarray):
        return v.tolist()
    return v


async def _get_prices_uncached(ticker: str, period: str, interval: str) -> dict:
    try:
        await _yfinance_limiter.acquire()
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period, interval=interval)
        records = _serialise_value(hist.reset_index().to_dict(orient="records"))
        return {"ticker": ticker, "period": period, "data": records}
    except Exception as exc:
        logger.warning("get_prices failed for %s: %s", ticker, exc)
        return {"ticker": ticker, "period": period, "error": str(exc), "data": []}


@app.tool()
@observe()
@logged()
async def get_prices(ticker: str, period: str = "1y", interval: str = "1d") -> dict:
    """Fetch OHLCV price history data for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        period: Time period. Options: 1d 5d 1mo 3mo 6mo 1y 2y 5y 10y ytd max
        interval: Data interval. Options: 1m 2m 5m 15m 30m 60m 90m 1h 1d 5d 1wk 1mo 3mo

    Returns:
        dict with keys: ticker, period, data (list of OHLCV records with ISO dates)
    """
    logger.info("Tool called", extra={"tool": "get_prices", "ticker": ticker})
    cache = _cache_benchmark if ticker.upper() in _BENCHMARK_TICKERS else _cache_prices
    return await cache.get_or_fetch(
        f"prices:{ticker.upper()}:{period}:{interval}",
        lambda: _get_prices_uncached(ticker, period, interval),
    )


async def _get_financials_uncached(ticker: str) -> dict:
    try:
        await _yfinance_limiter.acquire()
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
@observe()
@logged()
async def get_financials(ticker: str) -> dict:
    """Fetch financial statements and company info for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)

    Returns:
        dict with keys: income_statement, balance_sheet, cash_flow, info
    """
    logger.info("Tool called", extra={"tool": "get_financials", "ticker": ticker})
    return await _cache_financials.get_or_fetch(
        f"financials:{ticker.upper()}",
        lambda: _get_financials_uncached(ticker),
    )


_MACRO_TICKERS = {
    "us10y": "^TNX",      # 10-year Treasury yield
    "us2y":  "^FVX",      # 5-year proxy (closest clean 2Y in yfinance)
    "vix":   "^VIX",      # CBOE volatility index
    "dxy":   "DX-Y.NYB",  # US Dollar index
}

_SECTOR_ETFS = {
    "tech":          "XLK",
    "financials":    "XLF",
    "energy":        "XLE",
    "healthcare":    "XLV",
    "industrials":   "XLI",
    "consumer_disc": "XLY",
    "consumer_stap": "XLP",
    "utilities":     "XLU",
    "materials":     "XLB",
    "real_estate":   "XLRE",
    "communication": "XLC",
}

_cache_macro = make_cache(900, "macro")  # 15 min — macro moves slowly


async def _get_macro_impl() -> dict:
    await _yfinance_limiter.acquire()
    result: dict = {"macro": {}, "sectors": {}}
    for key, sym in _MACRO_TICKERS.items():
        try:
            hist = yf.Ticker(sym).history(period="1mo")
            if hist.empty:
                continue
            latest = float(hist["Close"].iloc[-1])
            prev   = float(hist["Close"].iloc[-5]) if len(hist) >= 5 else latest
            result["macro"][key] = {
                "value":         round(latest, 3),
                "change_5d_pct": round((latest - prev) / prev * 100, 2) if prev else 0,
            }
        except Exception as exc:
            result["macro"][key] = {"error": str(exc)}
    for name, sym in _SECTOR_ETFS.items():
        try:
            hist = yf.Ticker(sym).history(period="1mo")
            if hist.empty:
                continue
            latest = float(hist["Close"].iloc[-1])
            prev   = float(hist["Close"].iloc[-21]) if len(hist) >= 21 else latest
            result["sectors"][name] = round((latest - prev) / prev * 100, 2) if prev else 0
        except Exception:
            continue
    macro = result["macro"]
    if "us10y" in macro and "us2y" in macro:
        v10 = macro["us10y"].get("value", 0)
        v2  = macro["us2y"].get("value", 0)
        spread = v10 - v2
        result["macro"]["yield_curve_spread"] = round(spread, 3)
        result["macro"]["regime"] = (
            "inverted" if spread < 0 else "flat" if spread < 0.5 else "normal"
        )
    return result


@app.tool()
@observe()
async def get_macro_indicators() -> dict:
    """Fetch macro regime (Treasury yields, VIX, DXY) and sector ETF 1-month performance.

    Returns:
        dict with keys:
          macro: {us10y, us2y, vix, dxy, yield_curve_spread, regime}
          sectors: {tech, financials, energy, ...} (1-month % return)
    """
    return await _cache_macro.get_or_fetch("macro", _get_macro_impl)


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
        await _yfinance_limiter.acquire()
        stock = yf.Ticker(ticker)
        if expiration:
            chain = stock.option_chain(expiration)
            return _serialise_value({
                "calls": chain.calls.to_dict(orient="records"),
                "puts": chain.puts.to_dict(orient="records"),
            })
        return {"expirations": list(stock.options)}  # No expiration given — just list available dates.
    except Exception as exc:
        logger.warning("get_options_chain failed for %s: %s", ticker, exc)
        return {"ticker": ticker, "error": str(exc)}


# ──────────────────────────────────────────────
# SEC EDGAR Client
# ──────────────────────────────────────────────

# SEC EDGAR blocks requests without a valid User-Agent identifying the
# requester — their robots.txt enforcement actively rate-limits non-compliant
# clients. The contact email lets SEC reach us if we accidentally hammer them.
_SEC_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

# Only 10-K (annual) and 10-Q (quarterly) filings contain the full set of
# financial statements (balance sheet, income statement, cash flow). 8-Ks are
# unscheduled material events and are excluded from financial analysis.
FINANCIAL_FORM_TYPES: frozenset[str] = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})

# Forms 3/4/5 are insider ownership filings — their primaryDocument field
# points to an XSLT renderer rather than the actual filing HTML. For these
# we use the accession-number index page (the -index.htm URL) instead.
_INDEX_ONLY_FORMS: frozenset[str] = frozenset({"3", "4", "5", "3/A", "4/A", "5/A"})


class _EdgarClient:
    """Async SEC EDGAR client managing ticker→CIK maps and filing retrieval.

    Lazily initialises an httpx session, downloads the full SEC ticker map once,
    constructs filing URLs (edgar raw vs inline XBRL), and retries on failure.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._cik_cache: dict[str, str] = {}
        self._ticker_map: dict[str, str] | None = None      # {TICKER: cik_zfill10}
        self._title_map: dict[str, str] | None = None       # {TICKER: company title}
        self._ticker_map_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init httpx session with double-checked lock (one connection per process)."""
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
        # One-time download of ALL SEC-registered tickers (~10k entries at startup).
        async with self._ticker_map_lock:
            if self._ticker_map is not None:
                return self._ticker_map
            c = await self._get_client()
            await _sec_limiter.acquire()
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
            # Forms 3/4/5 primaryDocument is an XSLT stylesheet path, not an HTML filing.
# Using the index page avoids downloading a useless stylesheet that the agent
# would have to parse through with no useful content.
            edgar_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_short}/{acc_clean}/{acc_num}-index.htm"
            )
        else:
            edgar_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_short}/{acc_clean}/{safe_doc}"
            )

        # ix_url wraps the doc in SEC's inline XBRL viewer for structured data rendering.
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
                await _sec_limiter.acquire()
                resp = await c.get(url)
                resp.raise_for_status()
                result = resp.json()
                _cache_submissions.set(cik, result)
                return result
            except Exception:
                # Exponential backoff (1s, 2s) before raising on the 3rd failure.
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
        # SEC's JSON returns parallel arrays — bound-check against the shortest to avoid index errors.
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
        # Large filers (AAPL, MSFT) file dozens of 8-Ks per year — without pagination
        # we'd never reach the 10-Ks buried in the older pages.
        files_meta = data.get("filings", {}).get("files", [])
        for file_meta in files_meta:
            if len(annual) >= annual_limit and len(quarterly) >= quarterly_limit:
                break
            fname = file_meta.get("name", "")
            if not fname:
                continue
            try:
                c = await self._get_client()
                await _sec_limiter.acquire()
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
        """EDGAR full-text search via efts.sec.gov — indexes all filings."""
        try:
            params: dict = {"q": query}
            if ticker:
                params["cik"] = await self._lookup_cik(ticker)
            c = await self._get_client()
            await _sec_limiter.acquire()
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
        # Try edgar_url first, fall back to ix_url if the direct URL fails.
        tried_urls = [edgar_url]
        if ix_url and ix_url != edgar_url:
            tried_urls.append(ix_url)

        for url in tried_urls:
            try:
                c = await self._get_client()
                await _sec_limiter.acquire()
                resp = await c.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "")

                if "json" in content_type.lower():
                    text = json.dumps(resp.json(), indent=2)
                elif "xml" in content_type.lower() or url.endswith((".xml", ".xsd")):
                    # XBRL/XML filings — strip script/style/xbrldocument tags before text extraction.
                    try:
                        soup = BeautifulSoup(resp.text, "lxml-xml")
                    except Exception:
                        soup = BeautifulSoup(resp.text, "html.parser")
                    for tag in soup.find_all(["script", "style", "xbrldocument"]):
                        tag.decompose()
                    text = soup.get_text(separator=" ", strip=True)
                else:
                    # HTML filings — strip scripts, styles, noscripts to get clean readable text.
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for tag in soup(["script", "style", "noscript"]):
                        tag.decompose()
                    text = soup.get_text(separator=" ", strip=True)

                text = " ".join(text.split())
                # Truncate at 50 KB to keep context windows manageable for the LLM agent.
                if len(text) > 50000:
                    text = text[:50000] + "..."
                if len(text.strip()) < 100:
                    continue  # Empty result — try the next URL.
                return {"url": url, "content": text, "length": len(text)}
            except Exception as exc:
                logger.info("Tried %s, got: %s", url, exc)
                continue

        return {
            "url": edgar_url,
            "error": "Could not extract content from any URL",
            "content": "",
        }


# Module-level singleton — all MCP tools share one httpx session and one
# ticker map to avoid redundant network connections and SEC rate-limit hits.
_edgar = _EdgarClient()


# ──────────────────────────────────────────────
# SEC EDGAR MCP Tools
# ──────────────────────────────────────────────

@app.tool()
@observe()
@logged()
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
    logger.info("Tool called", extra={"tool": "get_company_filings", "ticker": ticker})
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
    # Filing content is immutable once filed — permanent LRU cache.
    cached = _cache_filing.get(edgar_url)
    if cached is not None:
        logger.debug("Cache hit: get_filing_content(%s)", edgar_url[:80])
        return cached
    try:
        result = await _edgar.get_filing_content(edgar_url, ix_url)
    except Exception as exc:
        logger.warning("get_filing_content tool failed: %s", exc)
        result = {"url": edgar_url, "error": str(exc), "content": ""}
    # Truncate server-side to cap bandwidth between MCP and RAG agent processes
    result["content"] = result.get("content", "")[:25000]
    if result.get("content"):
        _cache_filing.set(edgar_url, result)
    return result


# Guards against redundant pre-warms when multiple concurrent requests arrive
# before _prewarm_ticker_map() finishes.
_prewarm_done = False


async def _prewarm_ticker_map() -> None:
    """Pre-load the SEC ticker map at startup to avoid cold-start latency on first request.
    
    Without this, the first user query would pay a ~500ms penalty downloading
    the full SEC company_tickers.json (~10k entries) synchronously.
    """
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
    """Strip corporate suffixes and punctuation so "Apple Inc." -> "apple" matches "Apple". """
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
    """Match company name against the SEC ticker map with priority: exact→prefix→word_overlap→substring."""
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


import re as _re_module
# Most NYSE/Nasdaq tickers are 1-5 uppercase letters; 1-2 letter suffix for
# share classes (BRK.A, BF.B). Rejects crypto, currency pairs, indices.
_STOCK_TICKER_RE = _re_module.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")


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

        # Pass 2: any SEC-registered ticker (broader net than major exchanges).
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

        # Pass 3: any ticker that looks plausible (last resort).
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


# ──────────────────────────────────────────────
# Financial News Tools
# ──────────────────────────────────────────────

# VADER is rule-based (~1µs per headline), needs no GPU, no API keys, and
# performs well on financial news where standard lexicons often fail on
# domain-specific terms like "bearish", "beat estimates", "downgrade".
_sentiment_analyzer = SentimentIntensityAnalyzer()

# Three RSS feeds for broad financial news coverage (Yahoo, CNBC, MarketWatch).
# All free, no API keys required, and cover the major financial news wires.
_RSS_FEEDS: dict[str, str] = {
    "yahoo_finance": "https://finance.yahoo.com/news/rssindex",
    "cnbc_top": (
        "https://search.cnbc.com/rs/search/combinedcms/view.xml"
        "?partnerId=wrss01&id=100003114"
    ),
    "marketwatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
}


async def _fetch_rss(url: str, client: httpx.AsyncClient) -> dict:
    """Async RSS fetch — feedparser.parse is synchronous but the HTTP GET is async.

    Returns a dict with:
      entries (list): parsed feed entries, empty on failure
      status  (str):  "ok" | "http_{code}" | "error"
      error   (str):  error message if status != "ok", else ""
    """
    try:
        await _rss_limiter.acquire()
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
            # bozo flag means malformed XML — only reject if we got 0 entries.
            return {
                "entries": [],
                "status": "parse_error",
                "error": str(getattr(feed, "bozo_exception", "unknown parse error")),
            }
        return {"entries": feed.entries, "status": "ok", "error": ""}
    except Exception as exc:
        logger.warning("RSS fetch failed for %s: %s", url, exc)
        return {"entries": [], "status": "error", "error": str(exc)}


async def _fetch_ddg_news(ticker: str, company_name: str, limit: int = 10) -> list[dict]:
    """4th-tier news fallback via DuckDuckGo. Silently skips if `duckduckgo_search` is not installed.

    Returns a list of article dicts: {source, title, link, publisher, published, sentiment}.
    """
    try:
        from duckduckgo_search import DDGS  # optional dep
    except ImportError:
        return []
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _SA
        _sa = _SA()
        query = f"{company_name} {ticker} stock news" if company_name else f"{ticker} stock news"
        # DDGS().news() is sync; run in executor to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        def _search():
            with DDGS() as ddgs:
                return list(ddgs.news(query, max_results=limit, region="us-en"))
        await _rss_limiter.acquire()
        results = await loop.run_in_executor(None, _search)
        articles = []
        for item in results:
            title = item.get("title", "")
            body = item.get("body", "") or ""
            title_score = _sa.polarity_scores(title)["compound"]
            body_score = _sa.polarity_scores(body)["compound"] if body else title_score
            compound = round((title_score + body_score) / 2, 4)
            articles.append({
                "source": "duckduckgo",
                "title": title,
                "link": item.get("url", ""),
                "publisher": item.get("source", ""),
                "published": item.get("date", ""),
                "sentiment": compound,
            })
        return articles
    except Exception as exc:
        logger.warning("DuckDuckGo news fetch failed for %s: %s", ticker, exc)
        return []


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
        await _rss_limiter.acquire()
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


async def _get_news_sentiment_impl(ticker: str, limit: int) -> dict:
    """Core fetch logic for get_news_sentiment; called via single-flight cache."""
    keywords = await _resolve_company_keywords(ticker)
    articles: list[dict] = []
    scores: list[float] = []
    feed_status: dict[str, str] = {}

    # Merge generic feeds with a ticker-specific Yahoo Finance RSS (pre-filtered, no keyword match needed)
    ticker_feed_key = f"yahoo_ticker_{ticker.upper()}"
    ticker_feed_url = (
        f"https://feeds.finance.yahoo.com/rss/2.0/headline"
        f"?s={ticker.upper()}&region=US&lang=en-US"
    )
    all_feeds: dict[str, str] = {**_RSS_FEEDS, ticker_feed_key: ticker_feed_url}

    async with httpx.AsyncClient(headers=_SEC_HEADERS, follow_redirects=True) as client:
        rss_results = await asyncio.gather(
            *[_fetch_rss(url, client) for url in all_feeds.values()],
            return_exceptions=True,
        )

        for source, result in zip(all_feeds.keys(), rss_results):
            if isinstance(result, Exception):
                feed_status[source] = f"error: {result}"
                continue

            feed_status[source] = result["status"]
            if result["error"]:
                feed_status[source] += f" ({result['error']})"

            is_ticker_specific = source == ticker_feed_key
            for entry in result["entries"][:15]:
                title: str = entry.get("title", "")
                summary: str = entry.get("summary", "")
                # Ticker-specific feed is already filtered; generic feeds need keyword matching
                if not is_ticker_specific:
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

        rss_ok = any(v == "ok" for v in feed_status.values())
        if not articles:
            reason = "rss_unreachable" if not rss_ok else "rss_no_match"
            logger.info("RSS returned 0 articles for %s (%s), trying YF news API", ticker, reason)
            yf_articles = await _fetch_yf_news(ticker, client, limit=limit * 2)
            if yf_articles:
                articles = yf_articles[:limit]
                scores   = [a["sentiment"] for a in articles]
                feed_status["yahoo_finance_api"] = f"ok ({len(yf_articles)} articles)"
                source_used = f"yahoo_finance_api ({reason})"
            else:
                feed_status["yahoo_finance_api"] = "no articles returned"
                # 4th-tier fallback: DuckDuckGo news (skipped if duckduckgo_search not installed)
                company_name = ""
                try:
                    company_name = await _edgar.get_company_title(ticker.upper())
                except Exception:
                    pass
                ddg_articles = await _fetch_ddg_news(ticker, company_name, limit=limit)
                if ddg_articles:
                    articles = ddg_articles[:limit]
                    scores = [a["sentiment"] for a in articles]
                    feed_status["duckduckgo"] = f"ok ({len(ddg_articles)} articles)"
                    source_used = f"duckduckgo ({reason})"
                else:
                    feed_status["duckduckgo"] = "unavailable or 0 articles"
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
    return result


@app.tool()
@observe()
@logged()
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
    logger.info("Tool called", extra={"tool": "get_news_sentiment", "ticker": ticker})
    cache_key = f"news:{ticker.upper()}:{limit}"
    return await _cache_news.get_or_fetch(
        cache_key,
        lambda: _get_news_sentiment_impl(ticker, limit),
    )


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
        await _yfinance_limiter.acquire()
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


@app.tool()
@observe()
async def get_sentiment_indicators(ticker: str) -> dict:
    """Fetch positioning indicators: short interest, analyst consensus, institutional ownership.

    These are the structured signals the Quant agent's analyst_positioning_node consumes.
    Provided as a standalone MCP tool so external callers can query positioning without
    pulling the entire fundamentals payload.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)

    Returns:
        dict with keys:
          ticker,
          short_interest: {short_ratio, short_percent_of_float, shares_short},
          analyst: {recommendation_key, n_opinions, target_mean, target_high, target_low, current_price, upside_pct},
          institutional: {held_percent_institutions, held_percent_insiders}
    """
    try:
        await _yfinance_limiter.acquire()
        stock = yf.Ticker(ticker)
        info = stock.info or {}
        current = info.get("currentPrice") or info.get("regularMarketPrice")
        target_mean = info.get("targetMeanPrice")
        upside_pct = None
        if current and target_mean:
            try:
                upside_pct = round((float(target_mean) - float(current)) / float(current) * 100, 2)
            except Exception:
                upside_pct = None
        return {
            "ticker": ticker.upper(),
            "short_interest": {
                "short_ratio": _serialise_value(info.get("shortRatio")),
                "short_percent_of_float": _serialise_value(info.get("shortPercentOfFloat")),
                "shares_short": _serialise_value(info.get("sharesShort")),
                "shares_short_prior_month": _serialise_value(info.get("sharesShortPriorMonth")),
            },
            "analyst": {
                "recommendation_key": info.get("recommendationKey"),
                "n_opinions": _serialise_value(info.get("numberOfAnalystOpinions")),
                "target_mean": _serialise_value(target_mean),
                "target_high": _serialise_value(info.get("targetHighPrice")),
                "target_low": _serialise_value(info.get("targetLowPrice")),
                "current_price": _serialise_value(current),
                "upside_pct": upside_pct,
            },
            "institutional": {
                "held_percent_institutions": _serialise_value(info.get("heldPercentInstitutions")),
                "held_percent_insiders": _serialise_value(info.get("heldPercentInsiders")),
            },
        }
    except Exception as exc:
        logger.warning("get_sentiment_indicators failed for %s: %s", ticker, exc)
        return {"ticker": ticker.upper(), "error": str(exc)}


@app.tool()
@observe()
async def get_earnings_history(ticker: str, limit: int = 8) -> dict:
    """Fetch quarterly earnings history: EPS estimates vs actuals and surprise %.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        limit:  Number of past quarters to return (default 8 ≈ 2 years)

    Returns:
        dict with keys:
          ticker, n_quarters,
          quarters (list of {date, eps_estimate, eps_actual, surprise_pct}),
          beat_rate (fraction of quarters where actual > estimate),
          avg_surprise_pct
    """
    try:
        await _yfinance_limiter.acquire()
        stock = yf.Ticker(ticker)
        ed = stock.earnings_dates
        if ed is None or ed.empty:
            return {
                "ticker": ticker.upper(),
                "quarters": [],
                "beat_rate": None,
                "avg_surprise_pct": None,
                "n_quarters": 0,
            }
        # Filter to past quarters that have a reported EPS
        past = ed[ed["Reported EPS"].notna()].head(limit)
        quarters = []
        for dt, row in past.iterrows():
            quarters.append({
                "date": dt.isoformat(),
                "eps_estimate": _serialise_value(row.get("EPS Estimate")),
                "eps_actual": _serialise_value(row.get("Reported EPS")),
                "surprise_pct": _serialise_value(row.get("Surprise(%)")),
            })
        surprise_vals = [q["surprise_pct"] for q in quarters if q["surprise_pct"] is not None]
        beat_count = sum(1 for s in surprise_vals if s > 0)
        return {
            "ticker": ticker.upper(),
            "quarters": quarters,
            "beat_rate": round(beat_count / len(surprise_vals), 3) if surprise_vals else None,
            "avg_surprise_pct": round(sum(surprise_vals) / len(surprise_vals), 2) if surprise_vals else None,
            "n_quarters": len(quarters),
        }
    except Exception as exc:
        logger.warning("get_earnings_history failed for %s: %s", ticker, exc)
        return {"ticker": ticker.upper(), "error": str(exc), "quarters": []}


_cache_peers = make_cache(86400,   "peers")    # 24 hr — peers are stable intraday
_cache_shocks = make_cache(604800, "shocks")   # 7 days — historical shocks don't change

# Historical crash windows — (start, end) inclusive, used to compute actual returns.
# "mild_recession" uses the 2022 bear market as the most-recent well-defined drawdown.
_SCENARIO_WINDOWS: dict[str, tuple[str, str]] = {
    "market_crash_2008": ("2007-10-09", "2009-03-09"),
    "covid_crash_2020":  ("2020-02-19", "2020-03-23"),
    "dot_com_bubble":    ("2000-03-24", "2002-10-09"),
    "mild_recession":    ("2022-01-03", "2022-10-12"),
}
# Values used when live fetch fails or price history is too short.
_SHOCK_FALLBACKS: dict[str, float] = {
    "market_crash_2008": -0.565,   # S&P 500 actual peak-to-trough
    "covid_crash_2020":  -0.340,
    "dot_com_bubble":    -0.491,
    "mild_recession":    -0.254,   # 2022 S&P bear
}
# Sector-specific reference ETFs so defensive/growth names get appropriate benchmarks.
_SECTOR_ETF: dict[str, str] = {
    "Technology":             "QQQ",
    "Consumer Defensive":     "XLP",
    "Consumer Cyclical":      "XLY",
    "Healthcare":             "XLV",
    "Health Care":            "XLV",
    "Financial Services":     "XLF",
    "Financials":             "XLF",
    "Energy":                 "XLE",
    "Industrials":            "XLI",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Basic Materials":        "XLB",
    "Communication Services": "XLC",
}


@app.tool()
@observe()
async def get_insider_transactions(ticker: str, days: int = 90) -> dict:
    """Fetch recent insider buy/sell transactions using yfinance insider_transactions.

    More reliable than parsing SEC Form 4 filing titles — returns structured
    transaction type ("Sale", "Buy", "Option Exercise"), share counts, and values.

    Args:
        ticker: Stock ticker symbol (e.g. WMT, AAPL)
        days:   Look-back window in calendar days (default 90)

    Returns:
        dict with keys:
          ticker       — input symbol
          transactions — list of {insider, position, direction, shares, value, date, transaction}
          summary      — {total, buys, sells, direction, net_shares, net_value}
    """
    logger.info("Tool called", extra={"tool": "get_insider_transactions", "ticker": ticker})
    try:
        await _yfinance_limiter.acquire()
        stock = yf.Ticker(ticker.upper())
        df = stock.insider_transactions
        if df is None or df.empty:
            return {"ticker": ticker.upper(), "transactions": [], "summary": {"total": 0, "buys": 0, "sells": 0, "direction": "neutral"}}

        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
        # Start Date may be tz-aware or tz-naive
        start_col = df["Start Date"] if "Start Date" in df.columns else df.index
        try:
            mask = pd.to_datetime(start_col, utc=True) >= cutoff
        except Exception:
            mask = slice(None)
        recent = df[mask] if not isinstance(mask, slice) else df

        transactions = []
        buys = sells = 0
        net_shares = net_value = 0
        for _, row in recent.iterrows():
            txn = str(row.get("Transaction", ""))
            txn_lower = txn.lower()
            if any(w in txn_lower for w in ("buy", "purchase", "acquisition")):
                direction = "buy"
                buys += 1
                net_shares += int(row.get("Shares", 0) or 0)
                net_value += float(row.get("Value", 0) or 0)
            elif any(w in txn_lower for w in ("sale", "sell", "sold")):
                direction = "sell"
                sells += 1
                net_shares -= int(row.get("Shares", 0) or 0)
                net_value -= float(row.get("Value", 0) or 0)
            else:
                direction = "other"
            transactions.append({
                "insider": str(row.get("Insider", "")),
                "position": str(row.get("Position", "")),
                "direction": direction,
                "shares": int(row.get("Shares", 0) or 0),
                "value": float(row.get("Value", 0) or 0),
                "date": str(row.get("Start Date", ""))[:10],
                "transaction": txn,
            })

        if buys > sells and buys > 0:
            net_dir = "net_buy"
        elif sells > buys and sells > 0:
            net_dir = "net_sell"
        else:
            net_dir = "neutral"

        return {
            "ticker": ticker.upper(),
            "transactions": transactions[:20],
            "summary": {
                "total": len(transactions),
                "buys": buys,
                "sells": sells,
                "direction": net_dir,
                "net_shares": net_shares,
                "net_value": round(net_value, 2),
            },
        }
    except Exception as exc:
        logger.warning("get_insider_transactions failed for %s: %s", ticker, exc)
        return {"ticker": ticker.upper(), "transactions": [], "error": str(exc),
                "summary": {"total": 0, "buys": 0, "sells": 0, "direction": "neutral"}}


async def _get_scenario_shocks_uncached(sector: str) -> dict:
    """Compute historical crash returns from live price data.

    Tries the sector-specific ETF first, falls back to ^GSPC.
    For scenario windows that predate the ETF's inception (e.g. XLRE vs 2008),
    per-window fallback is applied automatically.
    """
    candidates = []
    etf = _SECTOR_ETF.get(sector, "")
    if etf:
        candidates.append(etf)
    candidates.append("^GSPC")

    prices_series: pd.Series | None = None
    index_used = "^GSPC"
    for sym in candidates:
        try:
            await _yfinance_limiter.acquire()
            hist = yf.Ticker(sym).history(period="max", interval="1d")
            if not hist.empty and len(hist) > 252:
                prices_series = hist["Close"].sort_index()
                index_used = sym
                break
        except Exception as exc:
            logger.debug("Shock fetch failed for %s: %s", sym, exc)

    shocks: dict[str, float] = {}
    for name, (start, end) in _SCENARIO_WINDOWS.items():
        if prices_series is not None:
            try:
                window = prices_series.loc[start:end]
                if len(window) >= 5:
                    shocks[name] = round(float(window.iloc[-1] / window.iloc[0] - 1), 4)
                    continue
            except Exception:
                pass
        # Per-window fallback to known S&P values
        shocks[name] = _SHOCK_FALLBACKS[name]

    source = "live" if any(v not in _SHOCK_FALLBACKS.values() for v in shocks.values()) else "fallback"
    return {"sector": sector or "market", "index_used": index_used, "shocks": shocks, "source": source}


@app.tool()
@observe()
async def get_scenario_shocks(sector: str = "") -> dict:
    """Return historical market-crash shock percentages for 4 scenarios.

    Uses the sector-specific ETF (QQQ for Tech, XLP for Consumer Defensive, etc.)
    so defensive and growth tickers get appropriate reference returns rather than
    the blended S&P 500 drawdown.  Falls back to ^GSPC when the ETF lacks history
    for a given window (e.g. XLRE doesn't cover the 2008 crash).

    Args:
        sector: Sector string from yfinance info (e.g. "Technology",
                "Consumer Defensive"). Pass empty string for S&P 500 baseline.

    Returns:
        dict with keys:
          sector     — the input sector
          index_used — the reference ETF/index that was fetched
          source     — "live" if fetched from price history, "fallback" if API failed
          shocks     — {scenario_name: decimal_return}  e.g. {"market_crash_2008": -0.47}
    """
    logger.info("Tool called", extra={"tool": "get_scenario_shocks", "sector": sector})
    cache_key = f"shocks:{sector or 'market'}"
    return await _cache_shocks.get_or_fetch(cache_key, lambda: _get_scenario_shocks_uncached(sector))


def _industry_to_slug(name: str) -> str:
    """Convert a yfinance industry/sector string to a yfinance URL slug."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


async def _get_peers_uncached(ticker: str) -> dict:
    """Fetch peer tickers via yfinance Industry/Sector classes.

    yfinance.Industry(slug).top_companies returns a market-cap-weighted
    DataFrame of companies in the same industry — no scraping, no cookies.
    Falls back to yf.Sector if the industry slug returns nothing.
    """
    ticker_up = ticker.upper()
    loop = asyncio.get_event_loop()

    def _fetch() -> list[str]:
        try:
            info = yf.Ticker(ticker_up).info
            industry = info.get("industry", "")
            sector = info.get("sector", "")
        except Exception:
            return []

        for name, cls in ((industry, yf.Industry), (sector, yf.Sector)):
            if not name:
                continue
            try:
                slug = _industry_to_slug(name)
                df = cls(slug).top_companies
                if df is not None and not df.empty:
                    return [s for s in df.index.tolist() if s and s != ticker_up][:8]
            except Exception as exc:
                logger.debug("yf.%s('%s') failed: %s", cls.__name__, name, exc)
        return []

    try:
        await _yfinance_limiter.acquire()
        peers = await loop.run_in_executor(None, _fetch)
        return {"ticker": ticker_up, "peers": peers}
    except Exception as exc:
        logger.warning("get_peers failed for %s: %s", ticker, exc)
        return {"ticker": ticker_up, "peers": [], "error": str(exc)}


@app.tool()
@observe()
async def get_peers(ticker: str) -> dict:
    """Return dynamically discovered peer/comparable tickers for any stock.

    Uses Yahoo Finance's recommendations-by-symbol API ("People also watch"),
    so peers are always current and work for any exchange-listed ticker.

    Args:
        ticker: Stock ticker symbol (e.g. WMT, NVDA, AAPL)

    Returns:
        dict with keys:
          ticker — the input symbol
          peers  — list of up to 8 similar ticker symbols
    """
    logger.info("Tool called", extra={"tool": "get_peers", "ticker": ticker})
    return await _cache_peers.get_or_fetch(
        f"peers:{ticker.upper()}",
        lambda: _get_peers_uncached(ticker),
    )


# ──────────────────────────────────────────────
# Hardened Python Sandbox (logic in shared/sandbox.py)
# ──────────────────────────────────────────────

from shared.sandbox import run_sandbox as _run_sandbox


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
    return await _run_sandbox(code, timeout)


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
    """Thread-safe lazy singleton for the ASGI server app.
    
    The Starlette app wraps the FastMCP SSE endpoint alongside a /health route.
    Lazily constructing it means uvicorn can import this module without
    eagerly loading starlette into memory until the server actually starts.
    Double-checked locking ensures only one caller creates the app wrapper.
    """
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


# Entry point for `python finsight_server.py`. Uvicorn is imported lazily here
# so that importing the module (e.g. for FastMCP SSE app refs) doesn't eagerly
# pull in the ASGI runtime — keeps import chains clean for agent tests.
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(get_app(), host=HOST, port=PORT)