import json
import logging
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
from scipy import stats

from .state import QuantAnalysisState

logger = logging.getLogger(__name__)


def _parse_price_data(mcp_result, ticker: str) -> dict:
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


def _get_fcf_from_financials(financials_dict: dict) -> float | None:
    for period_key, metrics in financials_dict.items():
        if "Free Cash Flow" in metrics:
            fcf = float(metrics["Free Cash Flow"])
            if fcf > 0:
                return fcf
    for period_key, metrics in financials_dict.items():
        op = metrics.get("Operating Cash Flow")
        capex = metrics.get("Capital Expenditure")
        if op is not None and capex is not None:
            fcf = float(op) + float(capex)
            if fcf > 0:
                return fcf
    return None


async def dcf_valuation_node(state: QuantAnalysisState) -> dict:
    ticker = state["ticker"]
    mcp = state.get("mcp_client")
    if not mcp:
        logger.warning("DCF skipped for %s: no MCP client", ticker)
        return {"dcf_valuation": None, "dcf_error": "No MCP client available"}

    try:
        result = await mcp.call_tool_by_name("get_financials", {"ticker": ticker})
        raw = ""
        if hasattr(result, "content") and result.content:
            raw = result.content[0].text if hasattr(result.content[0], "text") else str(result.content[0])
        if not raw:
            logger.warning("DCF skipped for %s: MCP returned empty response", ticker)
            return {"dcf_valuation": None, "dcf_error": "MCP returned empty response"}
        data = json.loads(raw)
        info = data.get("info", {})
        cash_flow_data = data.get("cash_flow", {})
        if not cash_flow_data:
            logger.warning("DCF skipped for %s: no cash flow data available", ticker)
            return {"dcf_valuation": None, "dcf_error": "No cash flow data available"}

        latest_fcf = _get_fcf_from_financials(cash_flow_data)
        if latest_fcf is None or latest_fcf <= 0:
            fcf_values = [float(m.get("Free Cash Flow", 0)) for m in cash_flow_data.values() if "Free Cash Flow" in m]
            op_values = [float(m.get("Operating Cash Flow", 0)) + float(m.get("Capital Expenditure", 0)) for m in cash_flow_data.values()]
            logger.warning(
                "DCF skipped for %s: no positive FCF found. FCF from FCF field: %s, FCF from OCF-CAPEX: %s",
                ticker, fcf_values, op_values
            )
            return {"dcf_valuation": None, "dcf_error": f"No positive FCF found. FCF values: {fcf_values}, OCF-CAPEX: {op_values}"}

        shares_outstanding = info.get("sharesOutstanding", 0)
        if not shares_outstanding or shares_outstanding <= 0:
            logger.warning("DCF skipped for %s: missing or invalid shares outstanding (%s)", ticker, shares_outstanding)
            return {"dcf_valuation": None, "dcf_error": f"Missing shares outstanding ({shares_outstanding})"}

        current_price = info.get("currentPrice") or info.get("regularMarketPrice", 0)
        if not current_price or current_price <= 0:
            logger.warning("DCF skipped for %s: missing or invalid current price (%s)", ticker, current_price)
            return {"dcf_valuation": None, "dcf_error": f"Missing current price ({current_price})"}

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

        intrinsic_value = enterprise_value / shares_outstanding if shares_outstanding and shares_outstanding > 0 else 0
        upside = (intrinsic_value - current_price) / current_price if current_price and intrinsic_value > 0 else 0

        logger.info(
            "DCF calculated for %s: FCF=%s, EV=%s, shares=%s, intrinsic=%s, current=%s, upside=%s%%",
            ticker, latest_fcf, enterprise_value, shares_outstanding, intrinsic_value, current_price, upside * 100
        )

        return {
            "dcf_valuation": {
                "intrinsic_value": round(intrinsic_value, 2),
                "current_price": round(float(current_price), 2) if current_price else 0,
                "upside_pct": round(float(upside * 100), 1),
                "wacc": wacc,
                "growth_rate": growth_rate,
                "terminal_growth": terminal_growth,
                "enterprise_value": round(enterprise_value, 2),
                "fcf_used": latest_fcf,
            }
        }
    except Exception as e:
        logger.warning("DCF failed for %s: %s", ticker, e)
        return {"dcf_valuation": None, "dcf_error": str(e)}


async def correlation_node(state: QuantAnalysisState) -> dict:
    prices_dict = state.get("price_data", {})
    holdings = state.get("portfolio_holdings", [])
    mcp = state.get("mcp_client")

    if not prices_dict or not holdings or not mcp:
        return {"correlation_matrix": {}}

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
            return {"correlation_matrix": {}}
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

    stress_test_info = None
    if stress:
        reasoning_parts.append(f"Stress test CVaR: {stress.get('cvar_95', 'N/A')}")
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
    }


async def llm_summary_node(state: QuantAnalysisState) -> dict:
    from langchain_openai import ChatOpenAI
    from shared.config import LLM_MODEL, LLM_BASE_URL

    metrics = state.get("metrics", {})
    stress = state.get("stress_test_result")
    dcf = state.get("dcf_valuation")
    ticker = state.get("ticker", "")
    rec = state.get("recommendation", "HOLD")
    reasoning = state.get("reasoning", "")

    today = date.today().isoformat()
    prompt = (
        f"Today's date: {today}. "
        f"You are a financial analyst. Summarize the following quantitative analysis for {ticker} "
        f"in 2-3 sentences for an investor.\n\n"
        f"Recommendation: {rec}\n"
        f"Key metrics: Sharpe={metrics.get('sharpe_ratio')}, "
        f"Volatility={metrics.get('annual_volatility')}, "
        f"Beta={metrics.get('beta')}, "
        f"VaR={metrics.get('var_95_daily')}\n"
        f"Reasoning: {reasoning}\n"
    )
    if dcf:
        prompt += f"DCF: intrinsic value=${dcf.get('intrinsic_value')}, upside={dcf.get('upside_pct')}%\n"
    if stress:
        prompt += f"Stress test CVaR: {stress.get('cvar_95')}\n"

    try:
        llm = ChatOpenAI(model=LLM_MODEL, base_url=LLM_BASE_URL, api_key="lmstudio", temperature=0.3, max_tokens=256)
        response = await llm.ainvoke(prompt)
        summary = response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        logger.warning("LLM summary failed: %s", e)
        summary = reasoning

    return {"reasoning": summary}
