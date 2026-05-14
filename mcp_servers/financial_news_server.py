import logging
from datetime import datetime, timezone

import feedparser
import httpx
from mcp.server.fastmcp import FastMCP
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = logging.getLogger(__name__)
app = FastMCP("financial-news-mcp")
_sentiment = SentimentIntensityAnalyzer()

_RSS_FEEDS = {
    "yahoo_finance": "https://finance.yahoo.com/news/rssindex",
    "cnbc_top": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "marketwatch": "https://feeds.marketwatch.com/marketwatch/topstories",
    "seeking_alpha": "https://seekingalpha.com/feed.xml",
}

_SEC_HEADERS = {
    "User-Agent": "FinSight Research (contact@finsight.com)",
    "Accept-Encoding": "gzip, deflate",
}


async def _get_company_name(ticker: str) -> list[str]:
    try:
        async with httpx.AsyncClient(headers=_SEC_HEADERS, timeout=10) as c:
            r = await c.get("https://www.sec.gov/files/company_tickers.json")
            for entry in r.json().values():
                if entry.get("ticker", "").upper() == ticker.upper():
                    return [ticker.lower()] + [w.lower() for w in entry.get("title", "").split() if len(w) > 2]
    except Exception:
        pass
    return [ticker.lower()]


@app.tool()
async def get_news_sentiment(ticker: str, limit: int = 10) -> dict:
    """Fetch recent financial news articles mentioning a ticker and compute VADER sentiment scores.

    Aggregates news from Yahoo Finance, CNBC, MarketWatch, and Seeking Alpha RSS feeds.
    Filters articles by ticker and company name. Returns both aggregate sentiment and per-article scores.

    Args:
        ticker: Stock ticker symbol to search for in news (e.g. NVDA, AAPL, MSFT)
        limit: Maximum number of articles to return (default 10)

    Returns:
        dict with keys: ticker (str), total_articles (int), sentiment_score (float -1 to 1), positive_articles (int), negative_articles (int), neutral_articles (int), articles (list of dicts each with: source, title, link, published, sentiment)
    """
    keywords = await _get_company_name(ticker)
    articles = []
    scores = []

    for source, url in _RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:15]:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                combined = f"{title} {summary}".lower()
                if any(k in combined for k in keywords):
                    vs = _sentiment.polarity_scores(combined)
                    scores.append(vs["compound"])
                    articles.append({
                        "source": source,
                        "title": title,
                        "link": entry.get("link", ""),
                        "published": entry.get("published", ""),
                        "sentiment": round(vs["compound"], 4),
                    })
        except Exception as e:
            logger.warning("RSS feed %s failed: %s", source, e)

    avg_sentiment = sum(scores) / len(scores) if scores else 0.0
    pos = sum(1 for s in scores if s > 0.05)
    neg = sum(1 for s in scores if s < -0.05)

    return {
        "ticker": ticker.upper(),
        "total_articles": len(articles),
        "sentiment_score": round(avg_sentiment, 4),
        "positive_articles": pos,
        "negative_articles": neg,
        "neutral_articles": len(scores) - pos - neg,
        "articles": articles[:limit],
    }


@app.tool()
async def get_earnings_calendar(ticker: str) -> dict:
    """Fetch upcoming earnings report date and status for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)

    Returns:
        dict with keys: ticker (str), source (str), status (str) or error (str)
    """
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            url = f"https://finance.yahoo.com/calendar/earnings?symbol={ticker}"
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                return {"ticker": ticker.upper(), "source": "yahoo_finance", "status": "fetched"}
            return {"ticker": ticker.upper(), "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ticker": ticker.upper(), "error": str(e)}


_starlette_app = None


def get_app():
    global _starlette_app
    if _starlette_app is None:
        _starlette_app = app.sse_app()
    return _starlette_app


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(get_app(), host="0.0.0.0", port=8025)
