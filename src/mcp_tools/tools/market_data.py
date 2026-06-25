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
    _YQ_LIMITER,
    BENCHMARK_TICKERS,
    cache_benchmark,
    cache_financials,
    cache_macro,
    cache_prices,
    cache_valuation_ts,
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
    # keyed "us2y" for backward-compat; actual instrument is 5-year
    # (^FVX) — yfinance has no clean 2Y future.
    "us2y": "^FVX",
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


async def _get_macro_impl_yfinance() -> dict:
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
            logger.debug("Failed to fetch sector data for %s", name, exc_info=True)
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


async def _get_macro_impl() -> dict:
    all_symbols = list(_MACRO_TICKERS.values()) + list(_SECTOR_ETFS.values())
    loop = asyncio.get_event_loop()

    try:
        from yahooquery import Ticker as YQTicker  # lazy import
        await _YQ_LIMITER.acquire()
        all_hist = await loop.run_in_executor(
            None, lambda: YQTicker(all_symbols).history(period="1mo")
        )
        if isinstance(all_hist, dict) and any("error" in str(v).lower() for v in all_hist.values()):
            raise ValueError("yahooquery returned errors for some symbols")
    except Exception as exc:
        logger.warning("yahooquery batch macro fetch failed, falling back to yfinance: %s", exc)
        return await _get_macro_impl_yfinance()

    result: dict = {"macro": {}, "sectors": {}}
    available_symbols = set(all_hist.index.get_level_values("symbol").unique())

    for key, sym in _MACRO_TICKERS.items():
        try:
            if sym not in available_symbols:
                continue
            hist = all_hist.xs(sym, level="symbol")
            if hist.empty:
                continue
            closes = hist["close"].sort_index()
            latest = float(closes.iloc[-1])
            prev = float(closes.iloc[-5]) if len(closes) >= 5 else latest
            result["macro"][key] = {
                "value": round(latest, 3),
                "change_5d_pct": round((latest - prev) / prev * 100, 2) if prev else 0,
            }
        except Exception as exc:
            result["macro"][key] = {"error": str(exc)}

    for name, sym in _SECTOR_ETFS.items():
        try:
            if sym not in available_symbols:
                continue
            hist = all_hist.xs(sym, level="symbol")
            if hist.empty:
                continue
            closes = hist["close"].sort_index()
            latest = float(closes.iloc[-1])
            prev = float(closes.iloc[-21]) if len(closes) >= 21 else latest
            result["sectors"][name] = round((latest - prev) / prev * 100, 2) if prev else 0
        except Exception:
            logger.debug("Failed to fetch yahooquery sector data for %s", name, exc_info=True)
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


# ──────────────────────────────────────────────
# Valuation timeseries
# ──────────────────────────────────────────────


async def _get_valuation_ts_uncached(ticker: str) -> dict:
    try:
        from yahooquery import Ticker as YQTicker  # lazy import
        await _YQ_LIMITER.acquire()
        loop = asyncio.get_event_loop()

        def _fetch() -> dict:
            t = YQTicker(ticker.upper())
            vm = t.valuation_measures
            if isinstance(vm, str):
                return {"error": vm}
            if hasattr(vm, "empty") and vm.empty:
                return {"periods": []}
            records = []
            for _, row in vm.iterrows():
                as_of = row.get("asOfDate", "")
                records.append({
                    "date": (
                        as_of.isoformat()[:10]
                        if hasattr(as_of, "isoformat")
                        else str(as_of)[:10]
                    ),
                    "period_type": row.get("periodType", ""),
                    "pe_ratio": _serialise_value(row.get("PeRatio")),
                    "ps_ratio": _serialise_value(row.get("PsRatio")),
                    "pb_ratio": _serialise_value(row.get("PbRatio")),
                    "peg_ratio": _serialise_value(row.get("PegRatio")),
                    "enterprise_value": _serialise_value(row.get("EnterpriseValue")),
                    "ev_to_ebitda": _serialise_value(row.get("EnterprisesValueEBITDARatio")),
                    "ev_to_revenue": _serialise_value(row.get("EnterprisesValueRevenueRatio")),
                    "market_cap": _serialise_value(row.get("MarketCap")),
                })
            return {"periods": records}

        data = await loop.run_in_executor(None, _fetch)
        if "error" in data:
            return {"ticker": ticker.upper(), "periods": [], "error": data["error"]}

        periods = data.get("periods", [])
        pe_values = [p["pe_ratio"] for p in periods if p["pe_ratio"] is not None]
        summary: dict = {}
        if pe_values:
            summary["pe_avg_2y"] = round(sum(pe_values) / len(pe_values), 2)
            summary["pe_current"] = pe_values[-1]
            summary["pe_percentile"] = round(
                sum(1 for v in pe_values if v <= pe_values[-1]) / len(pe_values) * 100, 1
            )

        return {
            "ticker": ticker.upper(),
            "periods": periods,
            "n_periods": len(periods),
            "summary": summary,
        }
    except Exception as exc:
        logger.warning("get_valuation_timeseries failed for %s: %s", ticker, exc)
        return {"ticker": ticker.upper(), "periods": [], "error": str(exc)}


@app.tool()
@observe()
@logged()
async def get_valuation_timeseries(ticker: str) -> dict:
    """Fetch quarterly valuation multiples history (P/E, P/S, PEG, EV) for a ticker.

    Returns up to ~8 quarters of trailing data with current-vs-average comparisons.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)

    Returns:
        dict with keys:
          ticker, periods (list of {date, pe_ratio, ps_ratio, pb_ratio, peg_ratio,
          enterprise_value, ev_to_ebitda, ev_to_revenue, market_cap}),
          n_periods, summary ({pe_avg_2y, pe_current, pe_percentile})
    """
    logger.info("Tool called", extra={"tool": "get_valuation_timeseries", "ticker": ticker})
    return await cache_valuation_ts.get_or_fetch(
        f"valuation_ts:{ticker.upper()}",
        lambda: _get_valuation_ts_uncached(ticker),
    )
