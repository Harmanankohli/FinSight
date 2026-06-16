"""Web search MCP tool via DuckDuckGo — general-purpose web search for all agents.

Unlike the news-only DDG fallback in ``infra/news_fetch.py``, this exposes
full web search (``DDGS.text()``) covering analyst reports, blog posts,
regulatory updates, Reddit discussions, and any real-time information that
structured data APIs miss.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from langfuse import observe

from mcp_tools._app import app
from mcp_tools.infra.rate_limiters import _WEB_SEARCH_LIMITER, cache_web_search
from shared.logging_config import logged

logger = logging.getLogger(__name__)


async def _web_search_uncached(query: str, max_results: int, time_filter: str) -> dict:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return {
            "query": query,
            "results": [],
            "error": "duckduckgo-search package not installed",
        }

    try:
        loop = asyncio.get_running_loop()

        def _search():
            with DDGS() as ddgs:
                kwargs = {"keywords": query, "max_results": max_results, "region": "us-en"}
                if time_filter and time_filter != "none":
                    kwargs["timelimit"] = time_filter
                return list(ddgs.text(**kwargs))

        await _WEB_SEARCH_LIMITER.acquire()
        raw_results = await loop.run_in_executor(None, _search)

        results = []
        for item in raw_results:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("href", ""),
                "snippet": item.get("body", ""),
            })

        return {
            "query": query,
            "total_results": len(results),
            "results": results,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.warning("Web search failed for query '%s': %s", query, exc)
        return {"query": query, "results": [], "error": str(exc)}


@app.tool()
@observe()
@logged()
async def web_search(query: str, max_results: int = 8, time_filter: str = "none") -> dict:
    """Search the web using DuckDuckGo for real-time information.

    Use this for analyst opinions, regulatory news, sector commentary,
    Reddit/forum discussions, blog posts, or any information not available
    through structured financial data APIs.

    Args:
        query: Search query string. Be specific for better results.
            Good: "AAPL iPhone 16 sales impact analyst opinion 2026"
            Bad: "Apple stock"
        max_results: Maximum number of results to return (1-20, default 8)
        time_filter: Time filter for results. Options:
            "none" — no filter (default)
            "d" — past day
            "w" — past week
            "m" — past month
            "y" — past year

    Returns:
        dict with keys: query, total_results, results (list of {title, url, snippet}), timestamp
    """
    logger.info("Tool called", extra={"tool": "web_search", "query": query})
    max_results = max(1, min(max_results, 20))
    if time_filter not in ("none", "d", "w", "m", "y"):
        time_filter = "none"
    cache_key = f"websearch:{query}:{max_results}:{time_filter}"
    return await cache_web_search.get_or_fetch(
        cache_key,
        lambda: _web_search_uncached(query, max_results, time_filter),
    )
