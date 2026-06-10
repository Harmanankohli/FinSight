"""SEC EDGAR MCP tools and client: _EdgarClient, get_company_filings, get_financial_filings,
full_text_search, get_filing_content.
"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import quote as urlquote

import httpx
from bs4 import BeautifulSoup
from langfuse import observe

from shared.settings import SEC_USER_AGENT
from shared.logging_config import logged

from mcp_servers._app import app
from mcp_servers.infra.rate_limiters import _EDGAR_LIMITER, cache_filing, cache_submissions

logger = logging.getLogger(__name__)

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


# ──────────────────────────────────────────────
# SEC EDGAR Client
# ──────────────────────────────────────────────

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
            await _EDGAR_LIMITER.acquire()
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
        cached = cache_submissions.get(cik)
        if cached is not None:
            logger.debug("Cache hit: _fetch_submissions(%s)", cik)
            return cached
        c = await self._get_client()
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        for attempt in range(3):
            try:
                await _EDGAR_LIMITER.acquire()
                resp = await c.get(url)
                resp.raise_for_status()
                result = resp.json()
                cache_submissions.set(cik, result)
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
                await _EDGAR_LIMITER.acquire()
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
            await _EDGAR_LIMITER.acquire()
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
                await _EDGAR_LIMITER.acquire()
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
    cached = cache_filing.get(edgar_url)
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
        cache_filing.set(edgar_url, result)
    return result
