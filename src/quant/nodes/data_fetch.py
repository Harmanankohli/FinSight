import json
import logging

from shared.logging_config import logged

from ..state import QuantAnalysisState
from .calculations import _parse_price_data

logger = logging.getLogger(__name__)


@logged()
async def fetch_price_data_node(state: QuantAnalysisState) -> dict:
    # First graph node: fetches historical daily closes via MCP get_prices tool into state["price_data"]  # noqa: E501
    ticker = state["ticker"]
    period = state.get("period", "5y")
    mcp = state.get("mcp_client")
    if not mcp:
        return {"price_data": {}}

    try:
        result = await mcp.call_tool_by_name(
            "get_prices", {"ticker": ticker, "period": period, "interval": "1d"}
        )
        prices = _parse_price_data(result, ticker)
        if not prices:
            raise ValueError(f"No price data found for {ticker}")
        return {"price_data": prices}
    except Exception as e:
        logger.error("Failed to fetch prices for %s: %s", ticker, e)
        return {"price_data": {}}


@logged()
async def fundamental_analysis_node(state: QuantAnalysisState) -> dict:
    """Fetches get_financials and extracts valuation, profitability, leverage, and growth ratios."""
    ticker = state["ticker"]
    mcp = state.get("mcp_client")
    if not mcp:
        return {"fundamentals": None, "_financials_raw": {}}
    try:
        result = await mcp.call_tool_by_name("get_financials", {"ticker": ticker})
        raw = ""
        if hasattr(result, "content") and result.content:
            raw = (
                result.content[0].text
                if hasattr(result.content[0], "text")
                else str(result.content[0])
            )
        if not raw:
            return {"fundamentals": None, "_financials_raw": {}}
        data = json.loads(raw)
        info = data.get("info", {})

        current_price = info.get("currentPrice") or info.get("regularMarketPrice", 0) or 0
        fifty2w_high = info.get("fiftyTwoWeekHigh") or 0
        fifty2w_low = info.get("fiftyTwoWeekLow") or 0
        fifty_day_ma = info.get("fiftyDayAverage") or 0
        two_hundred_day_ma = info.get("twoHundredDayAverage") or 0
        total_debt = info.get("totalDebt") or 0
        total_cash = info.get("totalCash") or 0

        fundamentals = {
            "trailing_pe": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "price_to_book": info.get("priceToBook"),
            "price_to_sales": info.get("priceToSalesTrailing12Months"),
            "ev_to_ebitda": info.get("enterpriseToEbitda"),
            "ev_to_revenue": info.get("enterpriseToRevenue"),
            "gross_margin": info.get("grossMargins"),
            "operating_margin": info.get("operatingMargins"),
            "profit_margin": info.get("profitMargins"),
            "roe": info.get("returnOnEquity"),
            "roa": info.get("returnOnAssets"),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "earnings_quarterly_growth": info.get("earningsQuarterlyGrowth"),
            "debt_to_equity": info.get("debtToEquity"),
            "current_ratio": info.get("currentRatio"),
            "quick_ratio": info.get("quickRatio"),
            "total_debt": total_debt,
            "total_cash": total_cash,
            "market_cap": info.get("marketCap"),
            "dividend_yield": info.get("dividendYield"),
            "payout_ratio": info.get("payoutRatio"),
            "52w_high": fifty2w_high,
            "52w_low": fifty2w_low,
            "50d_avg": fifty_day_ma,
            "200d_avg": two_hundred_day_ma,
            "current_price": current_price,
            "pct_from_52w_high": round((current_price - fifty2w_high) / fifty2w_high, 4)
            if fifty2w_high and current_price
            else None,
            "pct_from_52w_low": round((current_price - fifty2w_low) / fifty2w_low, 4)
            if fifty2w_low and current_price
            else None,
            "golden_cross": (fifty_day_ma > two_hundred_day_ma)
            if fifty_day_ma and two_hundred_day_ma
            else False,
            "net_debt": total_debt - total_cash,
        }
        return {"fundamentals": fundamentals, "_financials_raw": data}
    except Exception as e:
        logger.warning("Fundamentals failed for %s: %s", ticker, e)
        return {"fundamentals": None, "_financials_raw": {}}


@logged()
async def options_flow_node(state: QuantAnalysisState) -> dict:
    """Computes put/call volume and OI ratios from nearest-expiry options chain."""
    ticker = state["ticker"]
    mcp = state.get("mcp_client")
    if not mcp:
        return {"options_signals": None}
    try:
        result = await mcp.call_tool_by_name("get_options_chain", {"ticker": ticker})
        if not hasattr(result, "content") or not result.content:
            return {"options_signals": None}
        raw = (
            result.content[0].text if hasattr(result.content[0], "text") else str(result.content[0])
        )
        data = json.loads(raw)

        def _sum(rows: list, field: str) -> int:
            return sum(int(r.get(field) or 0) for r in rows if isinstance(r, dict))

        calls = data.get("calls", [])
        puts = data.get("puts", [])
        call_vol = _sum(calls, "volume")
        put_vol = _sum(puts, "volume")
        call_oi = _sum(calls, "openInterest")
        put_oi = _sum(puts, "openInterest")

        total_vol = call_vol + put_vol
        if total_vol == 0:
            # No volume — market closed or no active options chain; don't produce misleading ratios
            return {
                "options_signals": {
                    "put_call_volume_ratio": None,
                    "put_call_oi_ratio": round(put_oi / call_oi, 3) if call_oi > 0 else None,
                    "call_volume": 0,
                    "put_volume": 0,
                    "total_volume": 0,
                    "flow_signal": "no_data",
                    "note": "No options volume — market may be closed or chain is illiquid",
                }
            }

        pc_vol = round(put_vol / call_vol, 3) if call_vol > 0 else None
        pc_oi = round(put_oi / call_oi, 3) if call_oi > 0 else None

        if pc_vol is None:
            flow_signal = "no_data"
        elif pc_vol < 0.5:
            flow_signal = "bullish"
        elif pc_vol > 1.5:
            flow_signal = "bearish"
        else:
            flow_signal = "neutral"

        return {
            "options_signals": {
                "put_call_volume_ratio": pc_vol,
                "put_call_oi_ratio": pc_oi,
                "call_volume": call_vol,
                "put_volume": put_vol,
                "total_volume": total_vol,
                "flow_signal": flow_signal,
            }
        }
    except Exception as e:
        logger.warning("Options flow failed for %s: %s", ticker, e)
        return {"options_signals": None}


@logged()
async def insider_signals_node(state: QuantAnalysisState) -> dict:
    """Fetches insider buy/sell transactions via yfinance (structured data, not keyword matching)."""  # noqa: E501
    ticker = state["ticker"]
    mcp = state.get("mcp_client")
    if not mcp:
        return {"insider_signals": None}
    try:
        result = await mcp.call_tool_by_name(
            "get_insider_transactions", {"ticker": ticker, "days": 90}
        )
        if not hasattr(result, "content") or not result.content:
            return {"insider_signals": None}
        raw = (
            result.content[0].text if hasattr(result.content[0], "text") else str(result.content[0])
        )
        data = json.loads(raw)
        summary = data.get("summary", {})

        buys = summary.get("buys", 0)
        sells = summary.get("sells", 0)
        direction = summary.get("direction", "neutral")

        raw_fin = state.get("_financials_raw") or {}
        insider_pct = (raw_fin.get("info") or {}).get("heldPercentInsiders")

        total = summary.get("total", 0)
        return {
            "insider_signals": {
                "recent_transaction_count": total,
                "buy_signals": buys,
                "sell_signals": sells,
                "direction": direction,
                "net_shares": summary.get("net_shares"),
                "net_value": summary.get("net_value"),
                "insider_pct_held": insider_pct,
                "activity_level": "high" if total >= 5 else "moderate" if total >= 2 else "low",
            }
        }
    except Exception as e:
        logger.warning("Insider signals failed for %s: %s", ticker, e)
        return {"insider_signals": None}


@logged()
async def fetch_web_context_node(state: QuantAnalysisState) -> dict:
    """Fetches recent analyst/news web context via DuckDuckGo search."""
    ticker = state["ticker"]
    mcp = state.get("mcp_client")
    if not mcp:
        return {"web_context": []}
    try:
        result = await mcp.call_tool_by_name(
            "web_search",
            {
                "query": f"{ticker} stock analyst opinion news",
                "max_results": 5,
                "time_filter": "w",
            },
        )
        if not hasattr(result, "content") or not result.content:
            return {"web_context": []}
        raw = (
            result.content[0].text if hasattr(result.content[0], "text") else str(result.content[0])
        )
        data = json.loads(raw)
        results_list = [
            {"title": r.get("title", ""), "snippet": r.get("snippet", "")}
            for r in (data.get("results") or [])
        ]
        return {"web_context": results_list}
    except Exception as e:
        logger.warning("Web context fetch failed for %s: %s", ticker, e)
        return {"web_context": []}


@logged()
async def analyst_positioning_node(state: QuantAnalysisState) -> dict:
    """Extracts analyst consensus, price target upside, and short interest from pre-fetched financials."""  # noqa: E501
    raw_fin = state.get("_financials_raw") or {}
    if not raw_fin:
        return {"positioning": None}

    info = raw_fin.get("info", {})
    rec_key = info.get("recommendationKey") or ""
    n_analysts = info.get("numberOfAnalystOpinions")
    target_mean = info.get("targetMeanPrice")
    current_price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
    short_ratio = info.get("shortRatio")
    short_pct_float = info.get("shortPercentOfFloat")
    trailing_eps = info.get("trailingEps")
    forward_eps = info.get("forwardEps")

    consensus_map = {
        "strongbuy": 2,
        "strong_buy": 2,
        "buy": 1,
        "hold": 0,
        "neutral": 0,
        "underperform": -1,
        "sell": -2,
        "strongsell": -2,
        "strong_sell": -2,
    }
    consensus_score = consensus_map.get(rec_key.lower().replace(" ", ""), 0)

    analyst_upside = None
    if target_mean and current_price > 0:
        analyst_upside = round((target_mean - current_price) / current_price * 100, 1)

    earnings_surprise = None
    if trailing_eps and forward_eps and trailing_eps != 0:
        earnings_surprise = round((forward_eps - trailing_eps) / abs(trailing_eps), 3)

    return {
        "positioning": {
            "recommendation_key": rec_key,
            "consensus_score": consensus_score,
            "n_analysts": n_analysts,
            "analyst_target_price": target_mean,
            "analyst_upside_pct": analyst_upside,
            "short_ratio": short_ratio,
            "short_pct_float": short_pct_float,
            "earnings_surprise_est": earnings_surprise,
            "short_squeeze_risk": bool(short_ratio and short_ratio > 5),
        }
    }
