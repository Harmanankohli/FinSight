import json
import logging

import pandas as pd

from shared.data_freshness import get_latest_statement_date, statement_age_days, check_freshness
from shared.agent_models import DCFValuation
from shared.logging_config import logged
from shared.metrics import compute_cagr

from ..state import QuantAnalysisState
from .bank_valuation import run_bank_valuation
from .calculations import _get_fcf_from_financials, _get_latest_field, _run_monte_carlo

logger = logging.getLogger(__name__)


def _get_total_cash(info: dict, balance_sheet: dict) -> float:
    """Return total liquid assets from balance sheet for enterprise-value bridge.

    Includes cash, short-term investments, AND long-term marketable securities
    (e.g. AAPL holds ~$92B in non-current investment securities that are highly liquid).
    """
    for period_key in sorted(balance_sheet.keys(), reverse=True):
        bs = balance_sheet.get(period_key)
        if not isinstance(bs, dict):
            continue
        current_liquid = 0.0
        combined = bs.get("Cash Cash Equivalents And Short Term Investments")
        if combined is not None:
            try:
                current_liquid = float(combined)
            except (TypeError, ValueError):
                pass
        if current_liquid == 0:
            cash = bs.get("Cash And Cash Equivalents")
            sti = bs.get("Other Short Term Investments", 0)
            if cash is not None:
                try:
                    current_liquid = float(cash) + float(sti or 0)
                except (TypeError, ValueError):
                    pass
        if current_liquid <= 0:
            continue
        lt_investments = 0.0
        for field in ("Investments And Advances", "Long Term Equity Investment",
                       "Other Non Current Assets"):
            val = bs.get(field)
            if val is not None:
                try:
                    lt_investments = float(val)
                    if lt_investments > 0:
                        break
                except (TypeError, ValueError):
                    continue
        return current_liquid + lt_investments
    return float(info.get("totalCash", 0) or 0)


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


def _compute_sensitivity_matrix(
    latest_fcf: float, growth_rate: float, terminal_growth: float, wacc: float,
    total_debt: float, total_cash: float, shares_outstanding: float,
) -> dict[str, list[float | None]]:
    """Compute a WACC x terminal growth sensitivity grid."""
    tg_rates = [0.020, 0.025, 0.030, 0.035]
    wacc_rates = [0.08, 0.09, 0.10, 0.11, 0.12]
    matrix: dict[str, list[float | None]] = {}
    for tg in tg_rates:
        row: list[float | None] = []
        for w in wacc_rates:
            val = _compute_dcf_scenario(
                latest_fcf, growth_rate, tg, w,
                total_debt, total_cash, shares_outstanding,
                growth_adj=0.0, wacc_adj=0.0,
            )
            row.append(round(val, 2) if val else None)
        pct_label = f"{tg * 100:.1f}%"
        matrix[pct_label] = row
    return {
        "terminal_growth_labels": [f"{t * 100:.1f}%" for t in tg_rates],
        "wacc_labels": [f"{w * 100:.0f}%" for w in wacc_rates],
        "values": matrix,
    }


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

        # Historical CAGR priority: FCF CAGR → revenue CAGR → yfinance revenueGrowth → 8%.
        # earningsGrowth is a single-quarter YoY figure, not used for 5-year DCF growth.
        growth_rate = 0.08
        rg = None
        eg = None
        growth_method = "default_8pct"
        sorted_periods = sorted(cash_flow_data.keys())
        fcf_vals: list[float] = []
        for p in sorted_periods:
            period_data = cash_flow_data.get(p)
            if not isinstance(period_data, dict):
                continue
            # Try "Free Cash Flow" directly first
            fcf_raw = period_data.get("Free Cash Flow")
            if fcf_raw is not None:
                try:
                    v = float(fcf_raw)
                    if v > 0:
                        fcf_vals.append(v)
                        continue
                except (TypeError, ValueError):
                    pass
            # Fall back to OCF + CapEx for this period (CapEx is stored as negative)
            op = period_data.get("Operating Cash Flow")
            capex = period_data.get("Capital Expenditure")
            if op is not None and capex is not None:
                try:
                    v = float(op) + float(capex)
                    if v > 0:
                        fcf_vals.append(v)
                except (TypeError, ValueError):
                    pass
        if len(fcf_vals) >= 2:
            cagr = compute_cagr(fcf_vals)
            if cagr is not None and cagr >= 0:
                growth_rate = cagr
                growth_method = "fcf_cagr"
            elif cagr is not None:
                logger.info(
                    "FCF CAGR negative (%.3f) for %s — falling through to revenue growth",
                    cagr, ticker,
                )
        if growth_method == "default_8pct":
            sorted_income_periods = sorted(income_stmt.keys())
            rev_vals = [
                float(income_stmt[p].get("Total Revenue", 0))
                for p in sorted_income_periods
                if isinstance(income_stmt.get(p), dict)
                and income_stmt[p].get("Total Revenue") is not None
                and float(income_stmt[p]["Total Revenue"]) > 0
            ]
            if len(rev_vals) >= 2:
                cagr = compute_cagr(rev_vals)
                if cagr is not None:
                    growth_rate = cagr
                    growth_method = "revenue_cagr"
        # Blend historical CAGR with forward analyst estimate when both are available.
        # Pure historical CAGR can be dragged down by temporary flat periods (e.g. AAPL FY2022-23).
        rg = info.get("revenueGrowth")
        eg = info.get("earningsGrowth")
        if growth_method in ("fcf_cagr", "revenue_cagr") and rg is not None:
            forward = max(-0.20, min(float(rg), 0.25))
            growth_rate = 0.5 * growth_rate + 0.5 * forward
            growth_method = f"blended_{growth_method}"
        elif growth_method == "default_8pct":
            if rg is not None:
                growth_rate = max(-0.20, min(float(rg), 0.25))
                growth_method = "revenue_estimate"

        growth_rate = max(-0.20, min(growth_rate, 0.25))

        logger.info(
            "Growth for %s: method=%s, rate=%.3f", ticker, growth_method, growth_rate,
        )

        # WACC via CAPM cost-of-equity + after-tax cost-of-debt, weighted by capital structure
        beta = (state.get("metrics") or {}).get("beta") or 1.0
        mkt_premium = 0.047
        rf_annual = state.get("macro_data", {}).get("treasury_10y_yield")
        if rf_annual is None and mcp:
            try:
                macro_res = await mcp.call_tool_by_name("get_macro_indicators", {})
                if hasattr(macro_res, "content") and macro_res.content:
                    macro_txt = (
                        macro_res.content[0].text
                        if hasattr(macro_res.content[0], "text")
                        else str(macro_res.content[0])
                    )
                    macro_data = json.loads(macro_txt)
                    rf_annual = macro_data.get("treasury_10y_yield")
            except Exception as _mr:
                logger.debug("Macro data fetch failed (non-fatal): %s", _mr)
        if rf_annual is None:
            rf_annual = 4.3
        risk_free = rf_annual / 100
        cost_of_equity = risk_free + beta * mkt_premium

        interest_expense = _get_latest_field(income_stmt, "Interest Expense")
        total_debt = info.get("totalDebt") or 0
        market_cap = info.get("marketCap") or 0
        tax_rate = info.get("effectiveTaxRate")
        if not tax_rate:
            tax_provision = _get_latest_field(income_stmt, "Tax Provision")
            pretax_income = _get_latest_field(income_stmt, "Pretax Income")
            if tax_provision and pretax_income and pretax_income > 0:
                tax_rate = abs(tax_provision) / pretax_income
        if not tax_rate or tax_rate < 0 or tax_rate > 0.50:
            tax_rate = 0.21

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
        terminal_growth = min(sector_tg, wacc - 0.02) if growth_rate > 0.15 else min(sector_tg, 0.03)

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
        balance_sheet = data.get("balance_sheet", {})
        total_cash = _get_total_cash(info, balance_sheet)
        net_debt = total_debt - total_cash
        market_ev = market_cap + total_debt - total_cash if market_cap > 0 else None

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
            if valuation_ratio < 0.40:
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

        sensitivity = _compute_sensitivity_matrix(
            latest_fcf, growth_rate, terminal_growth, wacc,
            total_debt, total_cash, shares_outstanding,
        )

        return {
            "dcf_valuation": DCFValuation(
                intrinsic_value=round(intrinsic_value, 2),
                current_price=round(float(current_price), 2),
                upside_pct=round(float(upside * 100), 1),
                wacc=round(wacc, 4),
                growth_rate=round(growth_rate, 4),
                terminal_growth=round(terminal_growth, 4),
                enterprise_value=round(enterprise_value, 2),
                market_enterprise_value=round(market_ev, 2) if market_ev else None,
                net_debt=round(net_debt, 2),
                equity_value=round(equity_value, 2),
                fcf_used=latest_fcf,
                revenue_growth=round(rg, 4) if rg is not None else None,
                earnings_growth=round(eg, 4) if eg is not None else None,
                valuation_warnings=valuation_warnings,
                fcf_age_days=fcf_age_days,
                freshness=check_freshness(fcf_age_days),
                dcf_assumptions={
                    "risk_free_rate": round(risk_free, 4),
                    "beta": round(beta, 4),
                    "cost_of_equity": round(cost_of_equity, 4),
                    "cost_of_debt": round(after_tax_cod, 4),
                    "tax_rate": round(tax_rate, 4),
                    "fcf": latest_fcf,
                    "growth_rate": round(growth_rate, 4),
                    "terminal_growth": round(terminal_growth, 4),
                    "wacc": round(wacc, 4),
                    "shares_outstanding": shares_outstanding,
                    "net_debt": round(net_debt, 2),
                    "fcf_age_days": fcf_age_days,
                },
                scenarios=scenarios,
                valuation_reliability=confidence,
                sensitivity_matrix=sensitivity,
            ).model_dump(),
            "monte_carlo": mc,
        }
    except Exception as e:
        logger.warning("DCF failed for %s: %s", ticker, e)
        return {"dcf_valuation": None, "dcf_error": str(e)}
