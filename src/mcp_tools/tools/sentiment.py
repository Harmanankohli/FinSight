# ruff: noqa: E402, E501
"""Sentiment and market-signal MCP tools.

Tools: get_news_sentiment, get_earnings_calendar, get_sentiment_indicators,
       get_earnings_history, get_insider_transactions, get_scenario_shocks, get_peers.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pandas as pd
import yfinance as yf
from langfuse import observe

from mcp_tools._app import app
from mcp_tools.infra.news_fetch import (
    RSS_FEEDS,
    fetch_ddg_news,
    fetch_rss,
    fetch_yf_news,
    keyword_matches,
    normalise_for_match,
    resolve_company_keywords,
)
from mcp_tools.infra.rate_limiters import (
    _YF_LIMITER,
    cache_news,
    cache_peers,
    cache_shocks,
)
from mcp_tools.tools.edgar import _edgar
from mcp_tools.tools.market_data import _serialise_value
from shared.logging_config import logged
from shared.settings import SEC_USER_AGENT

logger = logging.getLogger(__name__)

_SEC_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

# VADER is rule-based (~1µs per headline), needs no GPU, no API keys, and
# performs well on financial news where standard lexicons often fail on
# domain-specific terms like "bearish", "beat estimates", "downgrade".
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_sentiment_analyzer = SentimentIntensityAnalyzer()

# ──────────────────────────────────────────────
# Scenario shock constants
# ──────────────────────────────────────────────

# Historical crash windows — (start, end) inclusive, used to compute actual returns.
# "mild_recession" uses the 2022 bear market as the most-recent well-defined drawdown.
_SCENARIO_WINDOWS: dict[str, tuple[str, str]] = {
    "market_crash_2008": ("2007-10-09", "2009-03-09"),
    "covid_crash_2020": ("2020-02-19", "2020-03-23"),
    "dot_com_bubble": ("2000-03-24", "2002-10-09"),
    "mild_recession": ("2022-01-03", "2022-10-12"),
}
# Values used when live fetch fails or price history is too short.
_SHOCK_FALLBACKS: dict[str, float] = {
    "market_crash_2008": -0.565,  # S&P 500 actual peak-to-trough
    "covid_crash_2020": -0.340,
    "dot_com_bubble": -0.491,
    "mild_recession": -0.254,  # 2022 S&P bear
}
# Sector-specific reference ETFs so defensive/growth names get appropriate benchmarks.
_SECTOR_ETF: dict[str, str] = {
    "Technology": "QQQ",
    "Consumer Defensive": "XLP",
    "Consumer Cyclical": "XLY",
    "Healthcare": "XLV",
    "Health Care": "XLV",
    "Financial Services": "XLF",
    "Financials": "XLF",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
    "Communication Services": "XLC",
}


# ──────────────────────────────────────────────
# News sentiment core logic
# ──────────────────────────────────────────────


async def _get_news_sentiment_impl(ticker: str, limit: int) -> dict:
    """Core fetch logic for get_news_sentiment; called via single-flight cache."""
    keywords = await resolve_company_keywords(ticker, _edgar)
    articles: list[dict] = []
    scores: list[float] = []
    feed_status: dict[str, str] = {}

    # Merge generic feeds with a ticker-specific Yahoo Finance RSS (pre-filtered, no keyword match needed)  # noqa: E501
    ticker_feed_key = f"yahoo_ticker_{ticker.upper()}"
    ticker_feed_url = (
        f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker.upper()}&region=US&lang=en-US"
    )
    all_feeds: dict[str, str] = {**RSS_FEEDS, ticker_feed_key: ticker_feed_url}

    async with httpx.AsyncClient(headers=_SEC_HEADERS, follow_redirects=True) as client:
        rss_results = await asyncio.gather(
            *[fetch_rss(url, client) for url in all_feeds.values()],
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
                    combined = normalise_for_match(f"{title} {summary}")
                    if not keyword_matches(combined, keywords):
                        continue

                title_score = _sentiment_analyzer.polarity_scores(title)["compound"]
                summary_score = (
                    _sentiment_analyzer.polarity_scores(summary)["compound"]
                    if summary
                    else title_score
                )
                compound = round((title_score + summary_score) / 2, 4)
                scores.append(compound)
                articles.append(
                    {
                        "source": source,
                        "title": title,
                        "link": entry.get("link", ""),
                        "published": entry.get("published", ""),
                        "sentiment": compound,
                    }
                )

        source_used = "rss"

        rss_ok = any(v == "ok" for v in feed_status.values())
        if not articles:
            reason = "rss_unreachable" if not rss_ok else "rss_no_match"
            logger.info("RSS returned 0 articles for %s (%s), trying YF news API", ticker, reason)
            yf_articles = await fetch_yf_news(ticker, client, limit=limit * 2)
            if yf_articles:
                articles = yf_articles[:limit]
                scores = [a["sentiment"] for a in articles]
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
                ddg_articles = await fetch_ddg_news(ticker, company_name, limit=limit)
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
        feeds_ok = [k for k, v in feed_status.items() if "ok" in v]
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


# ──────────────────────────────────────────────
# Scenario shocks helpers
# ──────────────────────────────────────────────


def _industry_to_slug(name: str) -> str:
    """Convert a yfinance industry/sector string to a yfinance URL slug."""
    import re

    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


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
    loop = asyncio.get_event_loop()
    for sym in candidates:
        try:
            await _YF_LIMITER.acquire()
            # history(period="max") fetches 25+ years of data — run in executor
            # to avoid blocking the event loop and starving concurrent MCP calls.
            hist = await loop.run_in_executor(
                None, lambda s=sym: yf.Ticker(s).history(period="max", interval="1d")
            )
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

    source = (
        "live" if any(v not in _SHOCK_FALLBACKS.values() for v in shocks.values()) else "fallback"
    )
    return {
        "sector": sector or "market",
        "index_used": index_used,
        "shocks": shocks,
        "source": source,
    }


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
        await _YF_LIMITER.acquire()
        peers = await loop.run_in_executor(None, _fetch)
        return {"ticker": ticker_up, "peers": peers}
    except Exception as exc:
        logger.warning("get_peers failed for %s: %s", ticker, exc)
        return {"ticker": ticker_up, "peers": [], "error": str(exc)}


# ──────────────────────────────────────────────
# Sentiment MCP Tools
# ──────────────────────────────────────────────


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
    return await cache_news.get_or_fetch(
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
        await _YF_LIMITER.acquire()
        loop = asyncio.get_event_loop()
        cal = await loop.run_in_executor(None, lambda: yf.Ticker(ticker).calendar)
        if cal and "Earnings Date" in cal:
            raw = cal["Earnings Date"]
            dates = raw if isinstance(raw, (list, tuple)) else [raw]
            iso_dates = [
                d.isoformat() if hasattr(d, "isoformat") else str(d) for d in dates if d is not None
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
        resp = await c.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
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
            filed_dates = sorted({e["filed"] for e in eps_data if "filed" in e}, reverse=True)
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
          analyst: {recommendation_key, n_opinions, target_mean, target_high, target_low, current_price, upside_pct},  # noqa: E501
          institutional: {held_percent_institutions, held_percent_insiders}
    """
    try:
        await _YF_LIMITER.acquire()
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, lambda: yf.Ticker(ticker).info) or {}
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
        await _YF_LIMITER.acquire()
        loop = asyncio.get_event_loop()
        ed = await loop.run_in_executor(None, lambda: yf.Ticker(ticker).earnings_dates)
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
            quarters.append(
                {
                    "date": dt.isoformat(),
                    "eps_estimate": _serialise_value(row.get("EPS Estimate")),
                    "eps_actual": _serialise_value(row.get("Reported EPS")),
                    "surprise_pct": _serialise_value(row.get("Surprise(%)")),
                }
            )
        surprise_vals = [q["surprise_pct"] for q in quarters if q["surprise_pct"] is not None]
        beat_count = sum(1 for s in surprise_vals if s > 0)
        return {
            "ticker": ticker.upper(),
            "quarters": quarters,
            "beat_rate": round(beat_count / len(surprise_vals), 3) if surprise_vals else None,
            "avg_surprise_pct": round(sum(surprise_vals) / len(surprise_vals), 2)
            if surprise_vals
            else None,
            "n_quarters": len(quarters),
        }
    except Exception as exc:
        logger.warning("get_earnings_history failed for %s: %s", ticker, exc)
        return {"ticker": ticker.upper(), "error": str(exc), "quarters": []}


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
        await _YF_LIMITER.acquire()
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(
            None, lambda: yf.Ticker(ticker.upper()).insider_transactions
        )
        if df is None or df.empty:
            return {
                "ticker": ticker.upper(),
                "transactions": [],
                "summary": {"total": 0, "buys": 0, "sells": 0, "direction": "neutral"},
            }

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
            transactions.append(
                {
                    "insider": str(row.get("Insider", "")),
                    "position": str(row.get("Position", "")),
                    "direction": direction,
                    "shares": int(row.get("Shares", 0) or 0),
                    "value": float(row.get("Value", 0) or 0),
                    "date": str(row.get("Start Date", ""))[:10],
                    "transaction": txn,
                }
            )

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
        return {
            "ticker": ticker.upper(),
            "transactions": [],
            "error": str(exc),
            "summary": {"total": 0, "buys": 0, "sells": 0, "direction": "neutral"},
        }


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
    return await cache_shocks.get_or_fetch(cache_key, lambda: _get_scenario_shocks_uncached(sector))


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
    return await cache_peers.get_or_fetch(
        f"peers:{ticker.upper()}",
        lambda: _get_peers_uncached(ticker),
    )
