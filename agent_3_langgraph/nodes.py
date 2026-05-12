import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf
from scipy import stats

from .state import QuantAnalysisState

logger = logging.getLogger(__name__)


async def fetch_price_data_node(state: QuantAnalysisState) -> dict:
    ticker = state["ticker"]
    period = state.get("period", "5y")
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period)
        if hist.empty:
            raise ValueError(f"No price data found for {ticker}")
        prices = hist["Close"].to_dict()
        prices = {str(k): float(v) for k, v in prices.items()}
        return {"price_data": prices}
    except Exception as e:
        logger.error("Failed to fetch prices for %s: %s", ticker, e)
        return {"price_data": {}}


async def compute_metrics_node(state: QuantAnalysisState) -> dict:
    prices_dict = state.get("price_data", {})
    if not prices_dict:
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
        sp500 = yf.Ticker("^GSPC")
        sp_hist = sp500.history(period=state.get("period", "5y"))
        if not sp_hist.empty:
            sp_returns = sp_hist["Close"].pct_change().dropna()
            common = returns.align(sp_returns, join="inner")
            if len(common[0]) > 1:
                cov = np.cov(common[0], common[1])
                beta = float(cov[0, 1] / cov[1, 1]) if cov[1, 1] > 0 else 1.0
            else:
                beta = 1.0
        else:
            beta = 1.0
    except Exception:
        beta = 1.0

    is_high = annual_vol > 0.35

    return {
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


async def stress_test_node(state: QuantAnalysisState) -> dict:
    prices_dict = state.get("price_data", {})
    if not prices_dict:
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


async def dcf_valuation_node(state: QuantAnalysisState) -> dict:
    ticker = state["ticker"]
    try:
        stock = yf.Ticker(ticker)
        info = stock.info or {}
        financials = stock.financials

        if financials is None or financials.empty:
            return {"dcf_valuation": None}

        if "Free Cash Flow" in financials.index:
            fcf = financials.loc["Free Cash Flow"]
        elif "Operating Cash Flow" in financials.index and "Capital Expenditure" in financials.index:
            fcf = financials.loc["Operating Cash Flow"] + financials.loc["Capital Expenditure"]
        else:
            return {"dcf_valuation": None}

        latest_fcf = float(fcf.iloc[0]) if not fcf.empty else 0
        if latest_fcf <= 0:
            return {"dcf_valuation": None}

        growth_rate = 0.08
        terminal_growth = 0.025
        wacc = 0.10
        num_years = 5

        pv_fcf = 0
        for year in range(1, num_years + 1):
            projected_fcf = latest_fcf * (1 + growth_rate) ** year
            pv_fcf += projected_fcf / (1 + wacc) ** year

        terminal_value = (latest_fcf * (1 + growth_rate) ** num_years * (1 + terminal_growth)) / (
            wacc - terminal_growth
        )
        pv_terminal = terminal_value / (1 + wacc) ** num_years
        enterprise_value = pv_fcf + pv_terminal

        shares_outstanding = info.get("sharesOutstanding", 0)
        if shares_outstanding and shares_outstanding > 0:
            intrinsic_value = enterprise_value / shares_outstanding
        else:
            intrinsic_value = 0

        current_price = info.get("currentPrice") or info.get("regularMarketPrice", 0)
        if current_price and intrinsic_value > 0:
            upside = (intrinsic_value - current_price) / current_price
        else:
            upside = 0

        return {
            "dcf_valuation": {
                "intrinsic_value": round(intrinsic_value, 2),
                "current_price": round(float(current_price), 2) if current_price else 0,
                "upside_pct": round(float(upside * 100), 1),
                "wacc": wacc,
                "growth_rate": growth_rate,
                "terminal_growth": terminal_growth,
                "enterprise_value": round(enterprise_value, 2),
            }
        }
    except Exception as e:
        logger.warning("DCF failed for %s: %s", ticker, e)
        return {"dcf_valuation": None}


async def correlation_node(state: QuantAnalysisState) -> dict:
    prices_dict = state.get("price_data", {})
    holdings = state.get("portfolio_holdings", [])

    if not prices_dict or not holdings:
        return {"correlation_matrix": {}}

    ticker = state["ticker"]
    all_tickers = [ticker] + holdings

    try:
        data = yf.download(all_tickers, period=state.get("period", "5y"))["Close"]
        returns = data.pct_change().dropna()
        corr = returns.corr()

        matrix = {}
        for t1 in all_tickers:
            matrix[t1] = {}
            for t2 in all_tickers:
                val = corr.loc[t1, t2] if t1 in corr.index and t2 in corr.columns else 0
                matrix[t1][t2] = round(float(val), 3)

        return {"correlation_matrix": matrix}
    except Exception as e:
        logger.warning("Correlation failed: %s", e)
        return {"correlation_matrix": {}}


async def format_output_node(state: QuantAnalysisState) -> dict:
    metrics = state.get("metrics", {})
    stress = state.get("stress_test_result")
    dcf = state.get("dcf_valuation")
    corr = state.get("correlation_matrix", {})

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

    pos_signals = sum(
        1 for s in signals if s in {"positive_risk_adjusted_return", "low_volatility", "undervalued_dcf"}
    )
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
        reasoning_parts.append(f"Sharpe: {metrics.get('sharpe_ratio', 'N/A')}, Vol: {metrics.get('annual_volatility', 'N/A')}, Beta: {metrics.get('beta', 'N/A')}")
    if dcf:
        reasoning_parts.append(f"DCF intrinsic value: ${dcf.get('intrinsic_value', 'N/A')} (upside: {dcf.get('upside_pct', 'N/A')}%)")
    if stress:
        reasoning_parts.append(f"Stress test CVaR: {stress.get('cvar_95', 'N/A')}")
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
        "stress_test_result": stress,
        "dcf_valuation": dcf,
        "correlation_matrix": corr,
    }
