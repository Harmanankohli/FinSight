import json
import logging
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
from scipy import stats

try:
    from langchain_community.cache import SQLiteCache
    from langchain_core.globals import set_llm_cache
    set_llm_cache(SQLiteCache(database_path="db/.langchain_cache.db"))
except Exception:
    logging.getLogger(__name__).warning("LangChain SQLiteCache unavailable; LLM caching disabled")

from .state import QuantAnalysisState

logger = logging.getLogger(__name__)


def _parse_price_data(mcp_result, ticker: str) -> dict:
    # Extract {date: close_price} dict from MCP tool response content (handles TextContent wrapping)
    if not hasattr(mcp_result, "content"):
        return {}
    for item in mcp_result.content:
        raw = item.text if hasattr(item, "text") else str(item)
        try:
            data = json.loads(raw)
            records = data.get("data", [])
            return {str(r.get("Date", r.get("date", ""))): float(r.get("Close", r.get("close", 0)))
                    for r in records if r.get("Date") or r.get("date")}
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return {}


async def fetch_price_data_node(state: QuantAnalysisState) -> dict:
    # First graph node: fetches historical daily closes via MCP get_prices tool into state["price_data"]
    ticker = state["ticker"]
    period = state.get("period", "5y")
    mcp = state.get("mcp_client")
    if not mcp:
        return {"price_data": {}}

    try:
        result = await mcp.call_tool_by_name("get_prices", {"ticker": ticker, "period": period, "interval": "1d"})
        prices = _parse_price_data(result, ticker)
        if not prices:
            raise ValueError(f"No price data found for {ticker}")
        return {"price_data": prices}
    except Exception as e:
        logger.error("Failed to fetch prices for %s: %s", ticker, e)
        return {"price_data": {}}


async def compute_metrics_node(state: QuantAnalysisState) -> dict:
    # Computes Sharpe ratio, annualized volatility, VaR (95%), max drawdown, and beta vs S&P 500
    prices_dict = state.get("price_data", {})
    ticker = state.get("ticker", "?")
    if not prices_dict:
        logger.warning("Metrics skipped for %s: no price data available", ticker)
        return {
            "volatility": 0.0,
            "is_high_volatility": False,
            "metrics": {
                "sharpe_ratio": 0.0,
                "annual_volatility": 0.0,
                "beta": 0.0,
                "var_95_daily": 0.0,
                "max_drawdown": 0.0,
            },
        }

    prices = pd.Series(
        {pd.Timestamp(k): v for k, v in prices_dict.items()}
    ).sort_index()
    returns = prices.pct_change().dropna()

    if len(returns) < 2:
        logger.warning("Metrics skipped for %s: only %d data points (need >= 2)", ticker, len(returns))
        return {
            "volatility": 0.0,
            "is_high_volatility": False,
            "metrics": {
                "sharpe_ratio": 0.0,
                "annual_volatility": 0.0,
                "beta": 0.0,
                "var_95_daily": 0.0,
                "max_drawdown": 0.0,
            },
        }

    annual_vol = float(returns.std() * np.sqrt(252))
    sharpe = float((returns.mean() / returns.std()) * np.sqrt(252)) if returns.std() > 0 else 0.0
    var_95 = float(np.percentile(returns, 5))

    running_max = prices.expanding().max()
    drawdown = (prices - running_max) / running_max
    max_dd = float(drawdown.min())

    try:
        mcp = state.get("mcp_client")
        sp_data = {}
        if mcp:
            sp_result = await mcp.call_tool_by_name("get_prices", {"ticker": "^GSPC", "period": state.get("period", "5y"), "interval": "1d"})
            sp_data = _parse_price_data(sp_result, "^GSPC")
        if sp_data:
            sp_series = pd.Series({pd.Timestamp(k): v for k, v in sp_data.items()}).sort_index()
            sp_returns = sp_series.pct_change().dropna()
            common = returns.align(sp_returns, join="inner")
            if len(common[0]) > 1:
                cov = np.cov(common[0], common[1])
                beta = float(cov[0, 1] / cov[1, 1]) if cov[1, 1] > 0 else 1.0
            else:
                beta = 1.0
        else:
            beta = 1.0
    except Exception as e:
        logger.warning("Beta calculation failed for %s: %s, defaulting to 1.0", ticker, e)
        beta = 1.0

    is_high = annual_vol > 0.35

    result = {
        "volatility": annual_vol,
        "is_high_volatility": is_high,
        "metrics": {
            "sharpe_ratio": round(sharpe, 3),
            "annual_volatility": round(annual_vol, 3),
            "beta": round(beta, 3),
            "var_95_daily": round(var_95, 4),
            "max_drawdown": round(max_dd, 4),
        },
    }
    if is_high:
        result["dcf_error"] = (
            f"DCF skipped: annual volatility ({annual_vol:.1%}) exceeds 35% threshold – "
            f"routed to stress test instead"
        )
    return result


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
            raw = result.content[0].text if hasattr(result.content[0], "text") else str(result.content[0])
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
            "pct_from_52w_high": round((current_price - fifty2w_high) / fifty2w_high, 4) if fifty2w_high and current_price else None,
            "pct_from_52w_low": round((current_price - fifty2w_low) / fifty2w_low, 4) if fifty2w_low and current_price else None,
            "golden_cross": (fifty_day_ma > two_hundred_day_ma) if fifty_day_ma and two_hundred_day_ma else None,
            "net_debt": total_debt - total_cash,
        }
        return {"fundamentals": fundamentals, "_financials_raw": data}
    except Exception as e:
        logger.warning("Fundamentals failed for %s: %s", ticker, e)
        return {"fundamentals": None, "_financials_raw": {}}


async def technical_analysis_node(state: QuantAnalysisState) -> dict:
    """Computes RSI, MACD, Bollinger, SMAs, EMAs, momentum, support/resistance from price_data."""
    prices_dict = state.get("price_data", {})
    ticker = state.get("ticker", "?")
    if not prices_dict:
        return {"technicals": None}
    try:
        prices = pd.Series(
            {pd.Timestamp(k): v for k, v in prices_dict.items()}
        ).sort_index()
        if len(prices) < 20:
            return {"technicals": None}

        current = float(prices.iloc[-1])

        sma20 = float(prices.rolling(20).mean().iloc[-1]) if len(prices) >= 20 else None
        sma50 = float(prices.rolling(50).mean().iloc[-1]) if len(prices) >= 50 else None
        sma200 = float(prices.rolling(200).mean().iloc[-1]) if len(prices) >= 200 else None

        ema12 = float(prices.ewm(span=12).mean().iloc[-1])
        ema26 = float(prices.ewm(span=26).mean().iloc[-1])
        macd_series = prices.ewm(span=12).mean() - prices.ewm(span=26).mean()
        macd_line = float(macd_series.iloc[-1])
        signal_line = float(macd_series.ewm(span=9).mean().iloc[-1])
        macd_histogram = macd_line - signal_line
        macd_bullish = macd_histogram > 0

        delta = prices.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi_raw = 100 - 100 / (1 + rs)
        rsi = float(rsi_raw.iloc[-1]) if not rsi_raw.empty and not np.isnan(rsi_raw.iloc[-1]) else 50.0

        bb_mid = prices.rolling(20).mean()
        bb_std = prices.rolling(20).std()
        bb_upper = float((bb_mid + 2 * bb_std).iloc[-1])
        bb_lower = float((bb_mid - 2 * bb_std).iloc[-1])
        bb_position = (current - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5

        mom_20d = float((current / prices.iloc[-20] - 1) * 100) if len(prices) >= 20 else None
        mom_60d = float((current / prices.iloc[-60] - 1) * 100) if len(prices) >= 60 else None

        recent = prices.tail(60)
        local_highs = recent[(recent.shift(1) < recent) & (recent.shift(-1) < recent)]
        local_lows = recent[(recent.shift(1) > recent) & (recent.shift(-1) > recent)]
        highs_above = local_highs[local_highs > current]
        lows_below = local_lows[local_lows < current]
        resistance = float(highs_above.min()) if len(highs_above) > 0 else None
        support = float(lows_below.max()) if len(lows_below) > 0 else None

        above_50 = current > sma50 if sma50 else False
        above_200 = current > sma200 if sma200 else False
        golden_cross = (sma50 > sma200) if (sma50 and sma200) else False

        if above_50 and above_200 and golden_cross:
            trend = "strong_uptrend"
        elif above_50 and above_200:
            trend = "uptrend"
        elif above_200 and not above_50:
            trend = "recovery"
        elif above_50 and not above_200:
            trend = "sideways"
        else:
            trend = "downtrend"

        return {
            "technicals": {
                "sma_20": round(sma20, 2) if sma20 else None,
                "sma_50": round(sma50, 2) if sma50 else None,
                "sma_200": round(sma200, 2) if sma200 else None,
                "ema_12": round(ema12, 2),
                "ema_26": round(ema26, 2),
                "macd": round(macd_line, 4),
                "macd_signal": round(signal_line, 4),
                "macd_histogram": round(macd_histogram, 4),
                "macd_bullish": macd_bullish,
                "rsi_14": round(rsi, 1),
                "bb_upper": round(bb_upper, 2),
                "bb_lower": round(bb_lower, 2),
                "bb_position": round(bb_position, 3),
                "momentum_20d": round(mom_20d, 2) if mom_20d is not None else None,
                "momentum_60d": round(mom_60d, 2) if mom_60d is not None else None,
                "support": round(support, 2) if support is not None else None,
                "resistance": round(resistance, 2) if resistance is not None else None,
                "trend": trend,
                "golden_cross": golden_cross,
                "above_50d_ma": above_50,
                "above_200d_ma": above_200,
            }
        }
    except Exception as e:
        logger.warning("Technical analysis failed for %s: %s", ticker, e)
        return {"technicals": None}


async def stress_test_node(state: QuantAnalysisState) -> dict:
    # Projects price under 4 historical crash scenarios (2008/2020/dot-com/recession) + CVaR of tail losses
    prices_dict = state.get("price_data", {})
    ticker = state.get("ticker", "?")
    if not prices_dict:
        logger.warning("Stress test skipped for %s: no price data", ticker)
        return {"stress_test_result": None}

    prices = pd.Series(
        {pd.Timestamp(k): v for k, v in prices_dict.items()}
    ).sort_index()
    returns = prices.pct_change().dropna()

    scenarios = {
        "market_crash_2008": -0.37,
        "covid_crash_2020": -0.34,
        "dot_com_bubble": -0.49,
        "mild_recession": -0.15,
    }

    results = {}
    current_price = float(prices.iloc[-1])
    for scenario, decline_pct in scenarios.items():
        projected = current_price * (1 + decline_pct)
        loss = projected - current_price
        results[scenario] = {
            "decline_pct": decline_pct,
            "projected_price": round(projected, 2),
            "loss_per_share": round(loss, 2),
        }

    var_95 = float(np.percentile(returns, 5))
    cvar_95 = float(returns[returns <= var_95].mean()) if len(returns[returns <= var_95]) > 0 else var_95

    return {
        "stress_test_result": {
            "scenarios": results,
            "var_95": round(var_95, 4),
            "cvar_95": round(cvar_95, 4),
        }
    }


def _get_latest_field(financials: dict, field: str) -> float | None:
    """Walk period-keyed dict (sorted descending), return most recent non-null value for field."""
    for period_key in sorted(financials.keys(), reverse=True):
        val = financials[period_key].get(field)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def _get_fcf_from_financials(financials_dict: dict) -> float | None:
    for period_key, metrics in financials_dict.items():
        if "Free Cash Flow" in metrics:
            fcf = float(metrics["Free Cash Flow"])
            logger.debug("FCF check %s: Free Cash Flow=%s", period_key, fcf)
            if fcf > 0:
                return fcf
    for period_key, metrics in financials_dict.items():
        op = metrics.get("Operating Cash Flow")
        capex = metrics.get("Capital Expenditure")
        if op is not None and capex is not None:
            fcf = float(op) + float(capex)
            logger.debug("FCF check %s: OCF=%s, CAPEX=%s, OCF-CAPEX=%s", period_key, op, capex, fcf)
            if fcf > 0:
                return fcf
    logger.debug("No positive FCF found across %d periods", len(financials_dict))
    return None


async def dcf_valuation_node(state: QuantAnalysisState) -> dict:
    """5-year tapered DCF with data-driven WACC and growth; reuses _financials_raw if available."""
    ticker = state["ticker"]
    mcp = state.get("mcp_client")

    # Reuse pre-fetched financials from fundamental_analysis_node when available
    data = state.get("_financials_raw") or {}
    if not data:
        if not mcp:
            return {"dcf_valuation": None, "dcf_error": "DCF skipped: MCP client not connected"}
        try:
            result = await mcp.call_tool_by_name("get_financials", {"ticker": ticker})
            raw = ""
            if hasattr(result, "content") and result.content:
                raw = result.content[0].text if hasattr(result.content[0], "text") else str(result.content[0])
            if not raw:
                return {"dcf_valuation": None, "dcf_error": "DCF skipped: financial data server returned empty response"}
            data = json.loads(raw)
        except Exception as e:
            return {"dcf_valuation": None, "dcf_error": str(e)}

    try:
        info = data.get("info", {})
        cash_flow_data = data.get("cash_flow", {})
        income_stmt = data.get("income_statement", {})

        if not cash_flow_data:
            return {"dcf_valuation": None, "dcf_error": "DCF skipped: no cash flow data available"}

        latest_fcf = _get_fcf_from_financials(cash_flow_data)
        if latest_fcf is None or latest_fcf <= 0:
            fcf_values = [float(m.get("Free Cash Flow", 0)) for m in cash_flow_data.values() if "Free Cash Flow" in m]
            op_values = [float(m.get("Operating Cash Flow", 0)) + float(m.get("Capital Expenditure", 0)) for m in cash_flow_data.values()]
            return {"dcf_valuation": None, "dcf_error": f"DCF skipped: no positive FCF (FCF={fcf_values}, OCF-CAPEX={op_values})"}

        shares_outstanding = info.get("sharesOutstanding", 0)
        if not shares_outstanding or shares_outstanding <= 0:
            return {"dcf_valuation": None, "dcf_error": f"DCF skipped: shares outstanding missing (value: {shares_outstanding})"}

        current_price = info.get("currentPrice") or info.get("regularMarketPrice", 0)
        if not current_price or current_price <= 0:
            return {"dcf_valuation": None, "dcf_error": f"DCF skipped: current price not available (value: {current_price})"}

        # Data-driven growth: blend revenue & earnings growth, bounded 2–25%
        rg = info.get("revenueGrowth")
        eg = info.get("earningsGrowth")
        if rg is not None and eg is not None:
            growth_rate = max(0.02, min(rg * 0.6 + eg * 0.4, 0.25))
        elif rg is not None:
            growth_rate = max(0.02, min(float(rg), 0.25))
        elif eg is not None:
            growth_rate = max(0.02, min(float(eg), 0.25))
        else:
            growth_rate = 0.08

        # WACC via CAPM cost-of-equity + after-tax cost-of-debt, weighted by capital structure
        beta = (state.get("metrics") or {}).get("beta") or 1.0
        risk_free, mkt_premium = 0.043, 0.055
        cost_of_equity = risk_free + beta * mkt_premium

        interest_expense = _get_latest_field(income_stmt, "Interest Expense")
        total_debt = info.get("totalDebt") or 0
        market_cap = info.get("marketCap") or 0
        tax_rate = info.get("effectiveTaxRate") or 0.21

        if interest_expense and total_debt > 0:
            after_tax_cod = (abs(interest_expense) / total_debt) * (1 - tax_rate)
        else:
            after_tax_cod = 0.04

        total_cap = market_cap + total_debt
        if total_cap > 0:
            wacc = (market_cap / total_cap) * cost_of_equity + (total_debt / total_cap) * after_tax_cod
        else:
            wacc = cost_of_equity
        wacc = max(0.06, min(wacc, 0.18))

        terminal_growth = min(0.03, wacc - 0.02) if growth_rate > 0.15 else 0.025

        # Tapered projection: fades toward terminal growth in years 3-5
        pv_fcf = 0
        for year in range(1, 6):
            if year <= 2:
                yr_growth = growth_rate
            else:
                fade = (year - 2) / 3
                yr_growth = growth_rate * (1 - fade) + terminal_growth * fade
            projected_fcf = latest_fcf * (1 + yr_growth) ** year
            pv_fcf += projected_fcf / (1 + wacc) ** year

        terminal_value = (latest_fcf * (1 + growth_rate) ** 5 * (1 + terminal_growth)) / (wacc - terminal_growth)
        pv_terminal = terminal_value / (1 + wacc) ** 5
        enterprise_value = pv_fcf + pv_terminal

        intrinsic_value = enterprise_value / shares_outstanding if shares_outstanding > 0 else 0
        upside = (intrinsic_value - current_price) / current_price if current_price and intrinsic_value > 0 else 0

        logger.info(
            "DCF for %s: FCF=%s, WACC=%.3f, growth=%.3f, terminal=%.3f, intrinsic=%s, upside=%.1f%%",
            ticker, latest_fcf, wacc, growth_rate, terminal_growth, intrinsic_value, upside * 100,
        )

        return {
            "dcf_valuation": {
                "intrinsic_value": round(intrinsic_value, 2),
                "current_price": round(float(current_price), 2),
                "upside_pct": round(float(upside * 100), 1),
                "wacc": round(wacc, 4),
                "growth_rate": round(growth_rate, 4),
                "terminal_growth": round(terminal_growth, 4),
                "enterprise_value": round(enterprise_value, 2),
                "fcf_used": latest_fcf,
            }
        }
    except Exception as e:
        logger.warning("DCF failed for %s: %s", ticker, e)
        return {"dcf_valuation": None, "dcf_error": str(e)}


async def correlation_node(state: QuantAnalysisState) -> dict:
    # Pairwise Pearson correlation matrix between primary ticker and portfolio holdings
    prices_dict = state.get("price_data", {})
    holdings = state.get("portfolio_holdings", [])
    mcp = state.get("mcp_client")

    if not prices_dict or not holdings or not mcp:
        return {"correlation_matrix": {"note": "No portfolio holdings provided. Include holdings like 'My portfolio holds AAPL, MSFT' to see correlation analysis."}}

    ticker = state["ticker"]
    all_tickers = [ticker] + holdings

    try:
        all_prices = {ticker: prices_dict}
        for h in holdings:
            result = await mcp.call_tool_by_name("get_prices", {"ticker": h, "period": state.get("period", "5y"), "interval": "1d"})
            hp = _parse_price_data(result, h)
            if hp:
                all_prices[h] = hp

        close_df = pd.DataFrame({t: pd.Series({pd.Timestamp(k): v for k, v in p.items()})
                                  for t, p in all_prices.items()}).sort_index()
        returns = close_df.pct_change().dropna()
        if returns.empty or len(returns.columns) < 2:
            return {"correlation_matrix": {"note": "Insufficient overlapping price data to compute correlations between holdings."}}
        corr = returns.corr()

        matrix = {}
        for t1 in all_tickers:
            if t1 not in corr.index:
                continue
            matrix[t1] = {}
            for t2 in all_tickers:
                if t2 not in corr.columns:
                    continue
                matrix[t1][t2] = round(float(corr.loc[t1, t2]), 3)

        return {"correlation_matrix": matrix}
    except Exception as e:
        logger.warning("Correlation failed: %s", e)
        return {"correlation_matrix": {"error": str(e)}}


async def format_output_node(state: QuantAnalysisState) -> dict:
    """Signal voting across risk, DCF, fundamentals, and technicals → BUY/HOLD/SELL."""
    metrics = state.get("metrics", {})
    stress = state.get("stress_test_result")
    dcf = state.get("dcf_valuation")
    corr = state.get("correlation_matrix", {})
    fundamentals = state.get("fundamentals")
    technicals = state.get("technicals")
    ticker = state.get("ticker", "?")
    dcf_error = state.get("dcf_error")

    logger.info("Formatting output for %s: dcf=%s, stress=%s, fundamentals=%s, technicals=%s",
                 ticker, "ok" if dcf else "null", "ok" if stress else "null",
                 "ok" if fundamentals else "null", "ok" if technicals else "null")

    sharpe = metrics.get("sharpe_ratio", 0)
    vol = metrics.get("annual_volatility", 0)

    signals = []
    if sharpe >= 1.0:
        signals.append("positive_risk_adjusted_return")
    elif sharpe < 0:
        signals.append("negative_risk_adjusted_return")

    if vol > 0.35:
        signals.append("high_volatility")
    elif vol < 0.15:
        signals.append("low_volatility")

    if dcf and dcf.get("upside_pct", 0) > 20:
        signals.append("undervalued_dcf")
    elif dcf and dcf.get("upside_pct", 0) < -20:
        signals.append("overvalued_dcf")

    if stress and stress.get("cvar_95", 0) < -0.05:
        signals.append("tail_risk")

    # Fundamental signals
    if fundamentals:
        pe = fundamentals.get("trailing_pe")
        if pe and pe > 0:
            if pe < 15:
                signals.append("low_pe_ratio")
            elif pe > 40:
                signals.append("high_pe_ratio")
        roe = fundamentals.get("roe")
        if roe is not None:
            if roe > 0.15:
                signals.append("strong_roe")
            elif roe < 0:
                signals.append("negative_roe")

    # Technical signals
    if technicals:
        trend = technicals.get("trend")
        if trend in ("strong_uptrend", "uptrend"):
            signals.append("bullish_trend")
        elif trend == "downtrend":
            signals.append("bearish_trend")
        rsi = technicals.get("rsi_14")
        if rsi is not None:
            if rsi < 30:
                signals.append("oversold_rsi")
            elif rsi > 70:
                signals.append("overbought_rsi")

    _POS = {"positive_risk_adjusted_return", "low_volatility", "undervalued_dcf",
             "low_pe_ratio", "strong_roe", "bullish_trend", "oversold_rsi"}
    pos_signals = sum(1 for s in signals if s in _POS)
    neg_signals = len(signals) - pos_signals

    if pos_signals > neg_signals:
        recommendation = "BUY"
    elif neg_signals > pos_signals:
        recommendation = "SELL"
    else:
        recommendation = "HOLD"

    confidence = max(0.0, min(1.0, (pos_signals + 1) / (len(signals) + 1) if signals else 0.5))

    reasoning_parts = []
    if metrics:
        reasoning_parts.append(
            f"Sharpe: {metrics.get('sharpe_ratio', 'N/A')}, "
            f"Vol: {metrics.get('annual_volatility', 'N/A')}, "
            f"Beta: {metrics.get('beta', 'N/A')}"
        )
    if dcf:
        reasoning_parts.append(f"DCF intrinsic: ${dcf.get('intrinsic_value', 'N/A')} (upside: {dcf.get('upside_pct', 'N/A')}%, WACC: {dcf.get('wacc', 'N/A'):.1%}, growth: {dcf.get('growth_rate', 'N/A'):.1%})")
    elif dcf_error:
        reasoning_parts.append(f"DCF: {dcf_error}")

    if fundamentals:
        fund_parts = []
        if fundamentals.get("trailing_pe") is not None:
            fund_parts.append(f"PE={fundamentals['trailing_pe']:.1f}")
        if fundamentals.get("roe") is not None:
            fund_parts.append(f"ROE={fundamentals['roe']:.1%}")
        if fundamentals.get("revenue_growth") is not None:
            fund_parts.append(f"RevGrowth={fundamentals['revenue_growth']:.1%}")
        if fundamentals.get("operating_margin") is not None:
            fund_parts.append(f"OpMargin={fundamentals['operating_margin']:.1%}")
        if fundamentals.get("debt_to_equity") is not None:
            fund_parts.append(f"D/E={fundamentals['debt_to_equity']:.1f}")
        if fund_parts:
            reasoning_parts.append(f"Fundamentals: {', '.join(fund_parts)}")

    if technicals:
        tech_parts = []
        if technicals.get("trend"):
            tech_parts.append(f"Trend={technicals['trend']}")
        if technicals.get("rsi_14") is not None:
            tech_parts.append(f"RSI={technicals['rsi_14']:.1f}")
        if technicals.get("macd_bullish") is not None:
            tech_parts.append(f"MACD={'bull' if technicals['macd_bullish'] else 'bear'}")
        if technicals.get("golden_cross") is not None:
            tech_parts.append(f"GoldenCross={technicals['golden_cross']}")
        if tech_parts:
            reasoning_parts.append(f"Technicals: {', '.join(tech_parts)}")

    stress_test_info = None
    if stress:
        reasoning_parts.append(f"Stress CVaR: {stress.get('cvar_95', 'N/A')}")
        stress_test_info = stress
    elif vol <= 0.35:
        stress_test_info = {
            "note": f"Stress test skipped - volatility ({vol:.1%}) below 35% threshold",
            "volatility": vol,
            "threshold": 0.35,
        }

    reasoning = " | ".join(reasoning_parts)

    return {
        "recommendation": recommendation,
        "reasoning": reasoning,
        "metrics": {
            **metrics,
            "quant_confidence": round(confidence, 3),
            "quant_signal": recommendation,
            "signals": signals,
        },
        "stress_test_result": stress_test_info,
        "dcf_valuation": dcf,
        "correlation_matrix": corr,
        "fundamentals": fundamentals,
        "technicals": technicals,
    }


async def llm_summary_node(state: QuantAnalysisState) -> dict:
    """Produces a 3-4 sentence investor summary including fundamentals and technicals."""
    from langchain_openai import ChatOpenAI
    from shared.config import LLM_MODEL, LLM_BASE_URL, LLM_API_KEY

    metrics = state.get("metrics", {})
    stress = state.get("stress_test_result")
    dcf = state.get("dcf_valuation")
    fund = state.get("fundamentals") or {}
    tech = state.get("technicals") or {}
    ticker = state.get("ticker", "")
    rec = state.get("recommendation", "HOLD")
    reasoning = state.get("reasoning", "")

    today = date.today().isoformat()
    prompt = (
        f"Today's date: {today}. "
        f"You are a financial analyst. Summarize the quantitative analysis for {ticker} "
        f"in 3-4 sentences for an investor.\n\n"
        f"Recommendation: {rec}\n"
        f"Risk: Sharpe={metrics.get('sharpe_ratio')}, "
        f"Vol={metrics.get('annual_volatility')}, "
        f"Beta={metrics.get('beta')}, MaxDD={metrics.get('max_drawdown')}\n"
    )
    if fund:
        prompt += (
            f"Fundamentals: PE={fund.get('trailing_pe')}, "
            f"ROE={fund.get('roe')}, D/E={fund.get('debt_to_equity')}, "
            f"RevGrowth={fund.get('revenue_growth')}, "
            f"OpMargin={fund.get('operating_margin')}\n"
        )
    if tech:
        prompt += (
            f"Technicals: Trend={tech.get('trend')}, "
            f"RSI={tech.get('rsi_14')}, MACD_bull={tech.get('macd_bullish')}, "
            f"GoldenCross={tech.get('golden_cross')}\n"
        )
    if dcf:
        prompt += (
            f"DCF: intrinsic=${dcf.get('intrinsic_value')}, "
            f"upside={dcf.get('upside_pct')}%, "
            f"WACC={dcf.get('wacc')}, growth={dcf.get('growth_rate')}\n"
        )
    if stress:
        prompt += f"Stress CVaR: {stress.get('cvar_95')}\n"
    prompt += "\nNote any signal conflicts. Be specific about numbers."

    try:
        llm = ChatOpenAI(model=LLM_MODEL, base_url=LLM_BASE_URL, api_key=LLM_API_KEY, temperature=0.3, max_tokens=384)
        response = await llm.ainvoke(prompt)
        summary = response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        logger.warning("LLM summary failed: %s", e)
        summary = reasoning

    return {"reasoning": summary}
