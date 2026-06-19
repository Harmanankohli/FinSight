import json
import logging

import pandas as pd

from shared.data_freshness import get_latest_statement_date, statement_age_days, check_freshness
from shared.logging_config import logged

from ..state import QuantAnalysisState
from .bank_valuation import run_bank_valuation
from .calculations import _get_fcf_from_financials, _get_latest_field, _run_monte_carlo

logger = logging.getLogger(__name__)

_TERMINAL_GROWTH = {
    "Technology": 0.03,
    "Communication Services": 0.03,
    "Consumer Defensive": 0.025,
    "Healthcare": 0.025,
    "Utilities": 0.02,
    "Energy": 0.02,
}


def _compute_dcf_scenario(
    latest_fcf: float,
    growth_rate: float,
    terminal_growth: float,
    wacc: float,
    total_debt: float,
    total_cash: float,
    shares_outstanding: float,
    growth_adj: float,
    wacc_adj: float,
) -> float | None:
    """Run a single DCF scenario with adjusted growth/WACC and return per-share value."""
    adj_growth = growth_rate * (1 + growth_adj)
    adj_wacc = max(0.06, min(wacc + wacc_adj, 0.18))
    adj_terminal = (
        min(terminal_growth, adj_wacc - 0.02)
        if adj_wacc > terminal_growth + 0.02
        else terminal_growth * 0.9
    )

    pv_fcf = 0
    projected_fcf = latest_fcf
    for year in range(1, 6):
        if year <= 2:
            yr_growth = adj_growth
        else:
            fade = (year - 2) / 3
            yr_growth = adj_growth * (1 - fade) + adj_terminal * fade
        projected_fcf *= 1 + yr_growth
        pv_fcf += projected_fcf / (1 + adj_wacc) ** year

    terminal_value = (projected_fcf * (1 + adj_terminal)) / (adj_wacc - adj_terminal)
    pv_terminal = terminal_value / (1 + adj_wacc) ** 5
    ev = pv_fcf + pv_terminal
    net_debt = total_debt - total_cash
    equity = ev - net_debt
    return equity / shares_outstanding if shares_outstanding > 0 else None


def _compute_dcf_confidence(
    current_price: float,
    intrinsic_value: float,
    growth_rate: float,
    wacc: float,
    fcf_age_days: float,
    has_fundamentals: bool,
    has_technicals: bool,
) -> float:
    """Score DCF confidence from 0-1 based on data quality and assumption stability."""
    data_quality = 0.5
    if has_fundamentals:
        data_quality += 0.25
    if has_technicals:
        data_quality += 0.25

    forecast_stability = 1.0
    valuation_ratio = (
        intrinsic_value / current_price
        if current_price and intrinsic_value > 0
        else 1.0
    )
    if valuation_ratio < 0.3 or valuation_ratio > 3.0:
        forecast_stability = 0.3
    elif valuation_ratio < 0.5 or valuation_ratio > 2.0:
        forecast_stability = 0.6

    assumption_reliability = 0.7
    if 0.05 <= wacc <= 0.12:
        assumption_reliability += 0.15
    if 0.02 <= growth_rate <= 0.15:
        assumption_reliability += 0.15
    assumption_reliability = min(assumption_reliability, 1.0)

    return round(0.4 * data_quality + 0.3 * forecast_stability + 0.3 * assumption_reliability, 2)


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

        sector = info.get("sector", "")
        _FINANCIAL_SECTORS = {"Financial Services", "Banking", "Financial"}
        if sector in _FINANCIAL_SECTORS:
            current_price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
            price_to_book = info.get("priceToBook")
            prices_dict = state.get("price_data", {})
            mc = None
            if prices_dict:
                prices_s = pd.Series(
                    {pd.Timestamp(k): v for k, v in prices_dict.items()}
                ).sort_index()
                mc = _run_monte_carlo(prices_s)
            return run_bank_valuation(
                ticker, sector, float(current_price), price_to_book, mc,
            )

        if not cash_flow_data:
            return {"dcf_valuation": None, "dcf_error": "DCF skipped: no cash flow data available"}

        latest_fcf = _get_fcf_from_financials(cash_flow_data)
        fcf_age_days = 0
        latest_date = get_latest_statement_date(cash_flow_data)
        if latest_date is not None:
            fcf_age_days = statement_age_days(latest_date)
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

        shares_outstanding = (
            info.get("impliedSharesOutstanding")
            or info.get("sharesOutstanding", 0)
        )
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

        logger.info(
            "Growth inputs for %s: revenue_growth=%s, earnings_growth=%s, final_growth=%.3f",
            ticker, rg, eg, growth_rate,
        )

        # WACC via CAPM cost-of-equity + after-tax cost-of-debt, weighted by capital structure
        beta = (state.get("metrics") or {}).get("beta") or 1.0
        risk_free, mkt_premium = 0.043, 0.055
        risk_free = (
            state.get("macro_data", {}).get("treasury_10y_yield", 4.3) / 100
        )
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

        sector_tg = _TERMINAL_GROWTH.get(sector, 0.025)
        terminal_growth = min(sector_tg, wacc - 0.02) if growth_rate > 0.15 else sector_tg

        # Tapered projection: fades toward terminal growth in years 3-5
        pv_fcf = 0
        projected_fcf = latest_fcf
        for year in range(1, 6):
            if year <= 2:
                yr_growth = growth_rate
            else:
                fade = (year - 2) / 3
                yr_growth = growth_rate * (1 - fade) + terminal_growth * fade
            projected_fcf *= 1 + yr_growth
            pv_fcf += projected_fcf / (1 + wacc) ** year

        terminal_value = (projected_fcf * (1 + terminal_growth)) / (wacc - terminal_growth)
        pv_terminal = terminal_value / (1 + wacc) ** 5
        enterprise_value = pv_fcf + pv_terminal

        total_debt = info.get("totalDebt", 0) or 0
        total_cash = info.get("totalCash", 0) or 0
        net_debt = total_debt - total_cash

        equity_value = enterprise_value - net_debt

        intrinsic_value = equity_value / shares_outstanding if shares_outstanding > 0 else 0
        upside = (
            (intrinsic_value - current_price) / current_price
            if current_price and intrinsic_value > 0
            else 0
        )

        # Sanity check: flag if DCF result deviates materially from market price
        valuation_warnings = []
        if current_price and intrinsic_value > 0:
            valuation_ratio = intrinsic_value / current_price
            if valuation_ratio < 0.30:
                valuation_warnings.append(
                    "DCF value significantly below market price"
                    " — review assumptions (WACC, growth, terminal)"
                )
            elif valuation_ratio > 3.0:
                valuation_warnings.append(
                    "DCF value significantly above market price"
                    " — review assumptions (WACC, growth, terminal)"
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

        # Multi-scenario DCF (Task 8)
        bear_value = _compute_dcf_scenario(
            latest_fcf, growth_rate, terminal_growth, wacc,
            total_debt, total_cash, shares_outstanding,
            growth_adj=-0.25, wacc_adj=0.01,
        )
        bull_value = _compute_dcf_scenario(
            latest_fcf, growth_rate, terminal_growth, wacc,
            total_debt, total_cash, shares_outstanding,
            growth_adj=0.25, wacc_adj=-0.01,
        )

        scenarios = {
            "bear": round(bear_value, 2) if bear_value else None,
            "base": round(intrinsic_value, 2),
            "bull": round(bull_value, 2) if bull_value else None,
        }

        # DCF Confidence Score (Task 9)
        confidence = _compute_dcf_confidence(
            current_price, intrinsic_value, growth_rate, wacc,
            fcf_age_days=fcf_age_days,
            has_fundamentals=bool(state.get("fundamentals")),
            has_technicals=bool(state.get("technicals")),
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
                "net_debt": round(net_debt, 2),
                "equity_value": round(equity_value, 2),
                "fcf_used": latest_fcf,
                "revenue_growth": round(rg, 4) if rg is not None else None,
                "earnings_growth": round(eg, 4) if eg is not None else None,
                "valuation_warnings": valuation_warnings,
                "fcf_age_days": fcf_age_days,
                "freshness": check_freshness(fcf_age_days),
                "dcf_assumptions": {
                    "fcf": latest_fcf,
                    "growth_rate": round(growth_rate, 4),
                    "terminal_growth": round(terminal_growth, 4),
                    "wacc": round(wacc, 4),
                    "shares_outstanding": shares_outstanding,
                    "net_debt": round(net_debt, 2),
                    "fcf_age_days": fcf_age_days,
                },
                "scenarios": scenarios,
                "confidence": confidence,
            },
            "monte_carlo": mc,
        }
    except Exception as e:
        logger.warning("DCF failed for %s: %s", ticker, e)
        return {"dcf_valuation": None, "dcf_error": str(e)}
