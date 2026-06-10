import json
import logging

import pandas as pd

from shared.logging_config import logged

from ..state import QuantAnalysisState
from .calculations import _get_fcf_from_financials, _get_latest_field, _run_monte_carlo

logger = logging.getLogger(__name__)


@logged()
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
                raw = (
                    result.content[0].text
                    if hasattr(result.content[0], "text")
                    else str(result.content[0])
                )
            if not raw:
                return {
                    "dcf_valuation": None,
                    "dcf_error": "DCF skipped: financial data server returned empty response",
                }
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
            fcf_values = [
                float(m.get("Free Cash Flow", 0))
                for m in cash_flow_data.values()
                if "Free Cash Flow" in m
            ]
            op_values = [
                float(m.get("Operating Cash Flow", 0)) + float(m.get("Capital Expenditure", 0))
                for m in cash_flow_data.values()
            ]
            return {
                "dcf_valuation": None,
                "dcf_error": f"DCF skipped: no positive FCF (FCF={fcf_values}, OCF-CAPEX={op_values})",  # noqa: E501
            }

        shares_outstanding = info.get("sharesOutstanding", 0)
        if not shares_outstanding or shares_outstanding <= 0:
            return {
                "dcf_valuation": None,
                "dcf_error": f"DCF skipped: shares outstanding missing (value: {shares_outstanding})",  # noqa: E501
            }

        current_price = info.get("currentPrice") or info.get("regularMarketPrice", 0)
        if not current_price or current_price <= 0:
            return {
                "dcf_valuation": None,
                "dcf_error": f"DCF skipped: current price not available (value: {current_price})",
            }

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
            wacc = (market_cap / total_cap) * cost_of_equity + (
                total_debt / total_cap
            ) * after_tax_cod
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

        terminal_value = (latest_fcf * (1 + growth_rate) ** 5 * (1 + terminal_growth)) / (
            wacc - terminal_growth
        )
        pv_terminal = terminal_value / (1 + wacc) ** 5
        enterprise_value = pv_fcf + pv_terminal

        intrinsic_value = enterprise_value / shares_outstanding if shares_outstanding > 0 else 0
        upside = (
            (intrinsic_value - current_price) / current_price
            if current_price and intrinsic_value > 0
            else 0
        )

        logger.info(
            "DCF for %s: FCF=%s, WACC=%.3f, growth=%.3f, terminal=%.3f, intrinsic=%s, upside=%.1f%%",  # noqa: E501
            ticker,
            latest_fcf,
            wacc,
            growth_rate,
            terminal_growth,
            intrinsic_value,
            upside * 100,
        )

        # Run Monte Carlo here so low-volatility tickers (routed to DCF, not stress test)
        # still get a GBM simulation. stress_test_node also runs it for high-vol tickers.
        prices_dict = state.get("price_data", {})
        mc = None
        if prices_dict:
            prices_s = pd.Series({pd.Timestamp(k): v for k, v in prices_dict.items()}).sort_index()
            mc = _run_monte_carlo(prices_s)

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
            },
            "monte_carlo": mc,
        }
    except Exception as e:
        logger.warning("DCF failed for %s: %s", ticker, e)
        return {"dcf_valuation": None, "dcf_error": str(e)}
