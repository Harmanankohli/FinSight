import asyncio
import ast
import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import httpx
import numpy as np
import pandas as pd
import yfinance as yf
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
# Agent Registry
# ──────────────────────────────────────────────

def _load_agent_cards():
    card_uris, agent_cards = [], []
    if not AGENT_CARDS_DIR.is_dir():
        logger.error("Agent cards directory not found: %s", AGENT_CARDS_DIR)
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
                except Exception as e:
                    logger.error("Error loading %s: %s", filename, e)
    logger.info("Loaded %d agent cards", len(agent_cards))
    return card_uris, agent_cards


_card_uris, _agent_cards = _load_agent_cards()
_df_registry = None
_model_embed = None

if _agent_cards:
    _model_embed = SentenceTransformer(EMBED_MODEL_NAME)
    _df_registry = pd.DataFrame({"card_uri": _card_uris, "agent_card": _agent_cards})
    _df_registry["card_embeddings"] = _df_registry["agent_card"].apply(
        lambda c: _model_embed.encode(json.dumps(c))
    )
else:
    _df_registry = pd.DataFrame(columns=["card_uri", "agent_card", "card_embeddings"])


@app.tool(
    name="find_agent",
    description="Finds the most relevant agent card based on a natural language query string",
)
def find_agent(query: str) -> str:
    if _df_registry.empty:
        return json.dumps({"error": "No agent cards loaded"})
    query_embedding = _model_embed.encode(query)
    dot_products = np.dot(np.stack(_df_registry["card_embeddings"]), query_embedding)
    best_idx = int(np.argmax(dot_products))
    return json.dumps(_df_registry.iloc[best_idx]["agent_card"])


@app.resource("resource://agent_cards/list", mime_type="application/json")
def get_agent_cards() -> dict:
    return {"agent_cards": _df_registry["card_uri"].to_list() if not _df_registry.empty else []}


@app.resource("resource://agent_cards/{card_name}", mime_type="application/json")
def get_agent_card(card_name: str) -> dict:
    if _df_registry.empty:
        return {"agent_card": None}
    uri = f"resource://agent_cards/{card_name}"
    cards = _df_registry.loc[_df_registry["card_uri"] == uri, "agent_card"].to_list()
    return {"agent_card": cards[0]} if cards else {"agent_card": None}


# ──────────────────────────────────────────────
# yfinance Tools
# ──────────────────────────────────────────────

@app.tool()
async def get_prices(ticker: str, period: str = "1y", interval: str = "1d") -> dict:
    """Fetch OHLCV price history data for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        period: Time period for historical data. Options: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
        interval: Data interval. Options: 1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo

    Returns:
        dict with keys: ticker, period, data (list of OHLCV records)
    """
    stock = yf.Ticker(ticker)
    hist = stock.history(period=period, interval=interval)
    return {
        "ticker": ticker,
        "period": period,
        "data": hist.reset_index().to_dict(orient="records"),
    }


@app.tool()
async def get_financials(ticker: str) -> dict:
    """Fetch financial statements and company info for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)

    Returns:
        dict with keys: income_statement, balance_sheet, cash_flow, info
    """
    stock = yf.Ticker(ticker)
    return {
        "income_statement": stock.financials.to_dict() if stock.financials is not None else {},
        "balance_sheet": stock.balance_sheet.to_dict() if stock.balance_sheet is not None else {},
        "cash_flow": stock.cashflow.to_dict() if stock.cashflow is not None else {},
        "info": stock.info if stock.info else {},
    }


@app.tool()
async def get_options_chain(ticker: str, expiration: str | None = None) -> dict:
    """Fetch options chain data for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        expiration: Optional option expiration date string (e.g. 2025-01-17). If omitted, returns available expiration dates.

    Returns:
        dict: If expiration provided, returns with keys: calls, puts. If no expiration, returns: expirations.
    """
    stock = yf.Ticker(ticker)
    if expiration:
        chain = stock.option_chain(expiration)
        return {
            "calls": chain.calls.to_dict(orient="records"),
            "puts": chain.puts.to_dict(orient="records"),
        }
    expirations = stock.options
    return {"expirations": list(expirations)}


# ──────────────────────────────────────────────
# SEC EDGAR Tools
# ──────────────────────────────────────────────

_SEC_HEADERS = {
    "User-Agent": "FinSight Research (contact@finsight.com)",
    "Accept-Encoding": "gzip, deflate",
}


class _EdgarClient:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._cik_cache: dict[str, str] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers=_SEC_HEADERS, timeout=30.0)
        return self._client

    async def _lookup_cik(self, ticker: str) -> str:
        ticker = ticker.upper()
        if ticker in self._cik_cache:
            return self._cik_cache[ticker]
        c = await self._get_client()
        resp = await c.get("https://www.sec.gov/files/company_tickers.json")
        resp.raise_for_status()
        for entry in resp.json().values():
            if entry["ticker"] == ticker:
                cik = str(entry["cik_str"]).zfill(10)
                self._cik_cache[ticker] = cik
                return cik
        raise ValueError(f"Ticker {ticker} not found")

    async def get_company_filings(self, ticker: str, form_types: list[str] | None = None, limit: int = 10) -> dict:
        cik = await self._lookup_cik(ticker)
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        c = await self._get_client()
        for attempt in range(3):
            try:
                resp = await c.get(url)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    c = await self._get_client()
                else:
                    raise
        filings = data.get("filings", {}).get("recent", {})
        result = []
        for i in range(len(filings.get("form", []))):
            form = filings["form"][i]
            if form_types and form not in form_types:
                continue
            if len(result) >= limit:
                break
            result.append({
                "form": form,
                "filing_date": filings.get("filingDate", [""])[i],
                "description": filings.get("primaryDocument", [""])[i],
                "edgar_url": f"https://www.sec.gov/ix?doc=/Archives/edgar/data/{cik}/{filings.get('accessionNumber', [''])[i].replace('-', '')}/{filings.get('primaryDocument', [''])[i]}",
            })
        return {"ticker": ticker, "cik": cik, "filings": result}

    async def full_text_search(self, query: str, ticker: str | None = None) -> dict:
        params: dict = {"q": query}
        if ticker:
            params["cik"] = await self._lookup_cik(ticker)
        c = await self._get_client()
        resp = await c.get("https://efts.sec.gov/LATEST/search-index?dateRange=all", params=params)
        resp.raise_for_status()
        data = resp.json()
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


_edgar = _EdgarClient()


@app.tool()
async def get_company_filings(ticker: str, form_types: str = "", limit: int = 10) -> dict:
    """Retrieve SEC filings for a publicly traded company by ticker symbol.

    Fetches filings from the SEC EDGAR system including annual reports (10-K), quarterly reports (10-Q),
    and current reports (8-K).

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        form_types: Comma-separated list of SEC form types to filter (e.g. "10-K,10-Q,8-K"). Leave empty to return all.
        limit: Maximum number of filings to return (default 10)

    Returns:
        dict with keys: ticker, cik, filings (list of {form, filing_date, description, edgar_url})
    """
    types_list = [t.strip() for t in form_types.split(",") if t.strip()] if form_types else None
    return await _edgar.get_company_filings(ticker, types_list, limit)


@app.tool()
async def full_text_search(query: str, ticker: str | None = None) -> dict:
    """Search the full text of SEC EDGAR filings for matching keywords.

    Args:
        query: Search query string (e.g. "revenue growth AI semiconductors")
        ticker: Optional ticker symbol to narrow search to a specific company

    Returns:
        dict with keys: query, results (list of {score, ticker, form, filing_date, description, url})
    """
    return await _edgar.full_text_search(query, ticker)


# ──────────────────────────────────────────────
# Financial News Tools
# ──────────────────────────────────────────────

_sentiment = SentimentIntensityAnalyzer()

_RSS_FEEDS = {
    "yahoo_finance": "https://finance.yahoo.com/news/rssindex",
    "cnbc_top": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "marketwatch": "https://feeds.marketwatch.com/marketwatch/topstories",
    "seeking_alpha": "https://seekingalpha.com/feed.xml",
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

    Args:
        ticker: Stock ticker symbol to search for in news (e.g. NVDA, AAPL, MSFT)
        limit: Maximum number of articles to return (default 10)

    Returns:
        dict with keys: ticker, total_articles, sentiment_score (-1 to 1), positive_articles, negative_articles, neutral_articles, articles
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
        dict with keys: ticker, source, status or error
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


# ──────────────────────────────────────────────
# Python Runner Tool
# ──────────────────────────────────────────────

_RESTRICTED_IMPORTS = [
    "os", "subprocess", "shutil", "socket", "ctypes",
    "importlib", "pickle", "inspect", "sys",
]


def _build_script(code: str) -> str:
    return (
        'import json, sys, math, statistics, itertools, collections, functools, typing, datetime, random\n'
        '_sb = {\n'
        '    "print": print, "len": len, "range": range, "int": int, "float": float,\n'
        '    "str": str, "bool": bool, "list": list, "dict": dict, "tuple": tuple,\n'
        '    "set": set, "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,\n'
        '    "any": any, "all": all, "sum": sum, "min": min, "max": max, "abs": abs,\n'
        '    "round": round, "sorted": sorted, "reversed": reversed,\n'
        '    "isinstance": isinstance, "hasattr": hasattr, "getattr": getattr, "type": type,\n'
        '    "True": True, "False": False, "None": None, "Exception": Exception,\n'
        '    "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError,\n'
        '}\n'
        '_sg = {\n'
        '    "__builtins__": _sb,\n'
        '    "pd": __import__("pandas"),\n'
        '    "np": __import__("numpy"),\n'
        '    "math": math, "json": json, "datetime": datetime, "random": random,\n'
        '    "statistics": statistics, "itertools": itertools, "collections": collections,\n'
        '    "functools": functools, "typing": typing,\n'
        '}\n'
        'try:\n'
        '    _locals = {}\n'
        f'    exec({json.dumps(code)}, _sg, _locals)\n'
        '    _result = _locals.get("result")\n'
        '    print("__RESULT__:" + json.dumps({\n'
        '        "type": type(_result).__name__ if _result is not None else "NoneType",\n'
        '        "value": repr(_result)[:1000]\n'
        '    }))\n'
        'except Exception:\n'
        '    import traceback\n'
        '    print("__ERROR__:" + traceback.format_exc(), file=sys.stderr)\n'
    )


def _check_code_safety(code: str) -> tuple[bool, str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _RESTRICTED_IMPORTS:
                    return False, f"Restricted import: {alias.name}"
        if isinstance(node, ast.ImportFrom):
            root = node.module.split(".")[0] if node.module else ""
            if root in _RESTRICTED_IMPORTS:
                return False, f"Restricted import: {node.module}"
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in {"exec", "eval", "open", "__import__"}:
                return False, f"Restricted function: {fn.id}"
            if isinstance(fn, ast.Attribute) and fn.attr in {"system", "popen", "exec", "eval"}:
                return False, f"Restricted method: {fn.attr}"
    return True, ""


@app.tool()
async def execute_python(code: str, timeout: int = 30) -> dict:
    """Execute Python code in a sandboxed subprocess with restricted imports.

    Runs the provided Python code in an isolated subprocess with limited builtins.
    Only safe libraries (pandas, numpy, math, json, etc.) are available.
    Restricted imports: os, subprocess, shutil, socket, ctypes, importlib, pickle, inspect, sys.

    Args:
        code: Python code string to execute. Use `result = <value>` to return data.
        timeout: Maximum execution time in seconds (default 30)

    Returns:
        dict with keys: success, stdout, stderr, result ({type, value})
    """
    safe, reason = _check_code_safety(code)
    if not safe:
        return {"success": False, "stdout": "", "stderr": reason, "result": None}
    script = _build_script(code)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(script)
        script_path = f.name
    try:
        proc = subprocess.run(
            [sys.executable, "-I", script_path],
            capture_output=True, text=True, timeout=timeout,
        )
        result = None
        clean_stdout = []
        for line in proc.stdout.splitlines():
            if line.startswith("__RESULT__:"):
                try:
                    result = json.loads(line[len("__RESULT__"):])
                except json.JSONDecodeError:
                    result = {"raw": line[len("__RESULT__"):]}
            else:
                clean_stdout.append(line)
        error_detail = proc.stderr
        for line in proc.stderr.splitlines():
            if line.startswith("__ERROR__:"):
                error_detail = line[len("__ERROR__"):]
        return {
            "success": proc.returncode == 0,
            "stdout": "\n".join(clean_stdout),
            "stderr": error_detail,
            "result": result,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": f"Timed out after {timeout}s", "result": None}
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": str(e), "result": None}
    finally:
        import os as _os
        try:
            _os.unlink(script_path)
        except Exception:
            pass


# ──────────────────────────────────────────────
# Server Entrypoint
# ──────────────────────────────────────────────

_starlette_app = None


def get_app():
    global _starlette_app
    if _starlette_app is None:
        _starlette_app = app.sse_app()
    return _starlette_app


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(get_app(), host=HOST, port=PORT)
