"""Market data MCP tools: get_prices, get_financials, get_macro_indicators, get_options_chain."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np
import yfinance as yf
from langfuse import observe

from mcp_tools._app import app
from mcp_tools.infra.rate_limiters import (
    _YF_LIMITER,
    BENCHMARK_TICKERS,
    cache_benchmark,
    cache_financials,
    cache_macro,
    cache_prices,
)
from shared.logging_config import logged

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Serialisation helper
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


# ──────────────────────────────────────────────
# Prices
# ──────────────────────────────────────────────


async def _get_prices_uncached(ticker: str, period: str, interval: str) -> dict:
    try:
        await _YF_LIMITER.acquire()
        loop = asyncio.get_event_loop()
        hist = await loop.run_in_executor(
            None, lambda: yf.Ticker(ticker).history(period=period, interval=interval)
        )
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
    cache = cache_benchmark if ticker.upper() in BENCHMARK_TICKERS else cache_prices
    return await cache.get_or_fetch(
        f"prices:{ticker.upper()}:{period}:{interval}",
        lambda: _get_prices_uncached(ticker, period, interval),
    )


# ──────────────────────────────────────────────
# Financials
# ──────────────────────────────────────────────


async def _get_financials_uncached(ticker: str) -> dict:
    try:
        await _YF_LIMITER.acquire()
        loop = asyncio.get_event_loop()

        def _fetch() -> dict:
            stock = yf.Ticker(ticker)
            return {
                "income_statement": stock.financials.to_dict()
                if stock.financials is not None
                else {},
                "balance_sheet": stock.balance_sheet.to_dict()
                if stock.balance_sheet is not None
                else {},
                "cash_flow": stock.cashflow.to_dict() if stock.cashflow is not None else {},
                "info": stock.info or {},
            }

        return _serialise_value(await loop.run_in_executor(None, _fetch))
    except Exception as exc:
        logger.warning("get_financials failed for %s: %s", ticker, exc)
        return {
            "ticker": ticker,
            "error": str(exc),
            "income_statement": {},
            "balance_sheet": {},
            "cash_flow": {},
            "info": {},
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
    return await cache_financials.get_or_fetch(
        f"financials:{ticker.upper()}",
        lambda: _get_financials_uncached(ticker),
    )


# ──────────────────────────────────────────────
# Macro indicators
# ──────────────────────────────────────────────

_MACRO_TICKERS = {
    "us10y": "^TNX",  # 10-year Treasury yield
    "us2y": "^FVX",  # 5-year proxy (closest clean 2Y in yfinance)
    "vix": "^VIX",  # CBOE volatility index
    "dxy": "DX-Y.NYB",  # US Dollar index
}

_SECTOR_ETFS = {
    "tech": "XLK",
    "financials": "XLF",
    "energy": "XLE",
    "healthcare": "XLV",
    "industrials": "XLI",
    "consumer_disc": "XLY",
    "consumer_stap": "XLP",
    "utilities": "XLU",
    "materials": "XLB",
    "real_estate": "XLRE",
    "communication": "XLC",
}


async def _get_macro_impl() -> dict:
    await _YF_LIMITER.acquire()
    loop = asyncio.get_event_loop()
    result: dict = {"macro": {}, "sectors": {}}
    for key, sym in _MACRO_TICKERS.items():
        try:
            hist = await loop.run_in_executor(
                None, lambda s=sym: yf.Ticker(s).history(period="1mo")
            )
            if hist.empty:
                continue
            latest = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-5]) if len(hist) >= 5 else latest
            result["macro"][key] = {
                "value": round(latest, 3),
                "change_5d_pct": round((latest - prev) / prev * 100, 2) if prev else 0,
            }
        except Exception as exc:
            result["macro"][key] = {"error": str(exc)}
    for name, sym in _SECTOR_ETFS.items():
        try:
            hist = await loop.run_in_executor(
                None, lambda s=sym: yf.Ticker(s).history(period="1mo")
            )
            if hist.empty:
                continue
            latest = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-21]) if len(hist) >= 21 else latest
            result["sectors"][name] = round((latest - prev) / prev * 100, 2) if prev else 0
        except Exception:
            continue
    macro = result["macro"]
    if "us10y" in macro and "us2y" in macro:
        v10 = macro["us10y"].get("value", 0)
        v2 = macro["us2y"].get("value", 0)
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
    return await cache_macro.get_or_fetch("macro", _get_macro_impl)


# ──────────────────────────────────────────────
# Options chain
# ──────────────────────────────────────────────


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
        await _YF_LIMITER.acquire()
        loop = asyncio.get_event_loop()

        def _fetch() -> dict:
            stock = yf.Ticker(ticker)
            if expiration:
                chain = stock.option_chain(expiration)
                return {
                    "calls": chain.calls.to_dict(orient="records"),
                    "puts": chain.puts.to_dict(orient="records"),
                }
            return {"expirations": list(stock.options)}

        return _serialise_value(await loop.run_in_executor(None, _fetch))
    except Exception as exc:
        logger.warning("get_options_chain failed for %s: %s", ticker, exc)
        return {"ticker": ticker, "error": str(exc)}
