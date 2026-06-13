# ruff: noqa: E501
"""News-fetching helpers: RSS, DuckDuckGo, Yahoo Finance news API.

These are pure helper functions with no MCP tool registrations.
They are consumed by ``mcp_tools.tools.sentiment``.
"""

from __future__ import annotations

import asyncio
import logging
import re as _re
from datetime import datetime, timezone
from urllib.parse import quote as urlquote

import feedparser
import httpx
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from mcp_tools.infra.rate_limiters import _NEWS_LIMITER

logger = logging.getLogger(__name__)

_sa = SentimentIntensityAnalyzer()

# Three RSS feeds for broad financial news coverage (Yahoo, CNBC, MarketWatch).
# All free, no API keys required, and cover the major financial news wires.
RSS_FEEDS: dict[str, str] = {
    "yahoo_finance": "https://finance.yahoo.com/news/rssindex",
    "cnbc_top": (
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"
    ),
    "marketwatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
}


async def fetch_rss(url: str, client: httpx.AsyncClient) -> dict:
    """Async RSS fetch — feedparser.parse is synchronous but the HTTP GET is async.

    Returns a dict with:
      entries (list): parsed feed entries, empty on failure
      status  (str):  "ok" | "http_{code}" | "error"
      error   (str):  error message if status != "ok", else ""
    """
    try:
        await _NEWS_LIMITER.acquire()
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


async def fetch_ddg_news(ticker: str, company_name: str, limit: int = 10) -> list[dict]:
    """4th-tier news fallback via DuckDuckGo. Silently skips if ``duckduckgo_search`` is not installed.  # noqa: E501

    Returns a list of article dicts: {source, title, link, publisher, published, sentiment}.
    """
    try:
        from duckduckgo_search import DDGS  # optional dep
    except ImportError:
        return []
    try:
        query = f"{company_name} {ticker} stock news" if company_name else f"{ticker} stock news"
        # DDGS().news() is sync; run in executor to avoid blocking the event loop
        loop = asyncio.get_running_loop()

        def _search():
            with DDGS() as ddgs:
                return list(ddgs.news(query, max_results=limit, region="us-en"))

        await _NEWS_LIMITER.acquire()
        results = await loop.run_in_executor(None, _search)
        articles = []
        for item in results:
            title = item.get("title", "")
            body = item.get("body", "") or ""
            title_score = _sa.polarity_scores(title)["compound"]
            body_score = _sa.polarity_scores(body)["compound"] if body else title_score
            compound = round((title_score + body_score) / 2, 4)
            articles.append(
                {
                    "source": "duckduckgo",
                    "title": title,
                    "link": item.get("url", ""),
                    "publisher": item.get("source", ""),
                    "published": item.get("date", ""),
                    "sentiment": compound,
                }
            )
        return articles
    except Exception as exc:
        logger.warning("DuckDuckGo news fetch failed for %s: %s", ticker, exc)
        return []


async def fetch_yf_news(ticker: str, client: httpx.AsyncClient, limit: int = 15) -> list[dict]:
    """Fetch news articles from Yahoo Finance search API for a ticker.

    This is a structured fallback when RSS feeds fail or return 0 articles.
    Unlike RSS, results are pre-filtered to the ticker — no keyword matching needed.

    Returns a list of article dicts: {title, link, publisher, published, summary}
    """
    try:
        url = (
            f"https://query1.finance.yahoo.com/v1/finance/search"
            f"?q={urlquote(ticker)}&lang=en-US&region=US&newsCount={limit}&quotesCount=0"
        )
        await _NEWS_LIMITER.acquire()
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
            title_score = _sa.polarity_scores(title)["compound"]
            summary_score = _sa.polarity_scores(summary)["compound"] if summary else title_score
            compound = round((title_score + summary_score) / 2, 4)
            articles.append(
                {
                    "source": "yahoo_finance_api",
                    "title": title,
                    "link": item.get("link", ""),
                    "publisher": item.get("publisher", ""),
                    "published": published,
                    "sentiment": compound,
                }
            )
        return articles
    except Exception as exc:
        logger.warning("YF news API failed for %s: %s", ticker, exc)
        return []


async def resolve_company_keywords(ticker: str, edgar_client: object) -> list[str]:
    """Build a robust keyword list for RSS headline matching.

    For JPM ("JPMORGAN CHASE & CO") produces:
      ["jpm", "jpmorgan", "chase", "jpmorgan chase", "jp morgan"]
    Covers "JPMorgan", "JP Morgan", "J.P. Morgan" after normalisation.
    """
    ticker_upper = ticker.upper()
    keywords: list[str] = [ticker.lower()]

    try:
        title = await edgar_client.get_company_title(ticker_upper)
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


def normalise_for_match(text: str) -> str:
    """Normalise a headline for keyword matching.

    Strips punctuation and collapses single-char tokens so that
    "J.P. Morgan" -> "jpmorgan" (matches keyword "jpmorgan").
    """
    s = _re.sub(r"[^a-z0-9 ]", " ", text.lower())
    s = _re.sub(r"\b([a-z])\b\s*", r"\1", s)
    return _re.sub(r"  +", " ", s).strip()


def keyword_matches(norm_text: str, keywords: list[str]) -> bool:
    """Word-boundary keyword match against a normalised headline.

    Uses \\b word boundaries for single-word keywords to avoid substring
    false positives (e.g. "chasing" matching keyword "chase").
    Multi-word keywords (bigrams) use substring match — they are already
    specific enough that a boundary check isn't needed.
    """
    for kw in keywords:
        if " " in kw:
            if kw in norm_text:
                return True
        else:
            if _re.search(rf"\b{_re.escape(kw)}\b", norm_text):
                return True
    return False
