"""Data fetching helpers for the quant graph.

Retrieves fundamentals, insider signals, analyst positioning,
and options data.
"""
import json
import logging

from shared.agent_models import (
    AnalystAction,
    AnalystPositioning,
    Fundamentals,
    InsiderSignals,
    OptionsSignals,
)
from shared.logging_config import logged

from ..state import QuantAnalysisState
from .calculations import _parse_price_data

logger = logging.getLogger(__name__)


def _get_total_liquid_cash(info: dict, balance_sheet: dict) -> float:
    """Return total liquid assets from balance sheet.

    Includes cash, short-term investments, AND long-term marketable securities
    (e.g. AAPL holds ~$92B in non-current investment securities).
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
        _total_cash = info.get("totalCash") or 0
        raw_de = info.get("debtToEquity")

        balance_sheet = data.get("balance_sheet", {})
        liquid_cash = _get_total_liquid_cash(info, balance_sheet)

        fundamentals = Fundamentals(
            trailing_pe=info.get("trailingPE"),
            forward_pe=info.get("forwardPE"),
            price_to_book=info.get("priceToBook"),
            price_to_sales=info.get("priceToSalesTrailing12Months"),
            ev_to_ebitda=info.get("enterpriseToEbitda"),
            ev_to_revenue=info.get("enterpriseToRevenue"),
            gross_margin=info.get("grossMargins"),
            operating_margin=info.get("operatingMargins"),
            profit_margin=info.get("profitMargins"),
            roe=info.get("returnOnEquity"),
            roa=info.get("returnOnAssets"),
            revenue_growth=info.get("revenueGrowth"),
            earnings_growth=info.get("earningsGrowth"),
            earnings_quarterly_growth=info.get("earningsQuarterlyGrowth"),
            debt_to_equity=raw_de / 100 if raw_de is not None else None,
            current_ratio=info.get("currentRatio"),
            quick_ratio=info.get("quickRatio"),
            total_debt=total_debt,
            total_cash=liquid_cash,
            market_cap=info.get("marketCap"),
            dividend_yield=info.get("dividendYield"),
            payout_ratio=info.get("payoutRatio"),
            high_52w=fifty2w_high,
            low_52w=fifty2w_low,
            avg_50d=fifty_day_ma,
            avg_200d=two_hundred_day_ma,
            current_price=current_price,
            pct_from_52w_high=round((current_price - fifty2w_high) / fifty2w_high, 4)
            if fifty2w_high and current_price
            else None,
            pct_from_52w_low=round((current_price - fifty2w_low) / fifty2w_low, 4)
            if fifty2w_low and current_price
            else None,
            net_debt=total_debt - liquid_cash,
            sector=info.get("sector"),
            industry=info.get("industry"),
        ).model_dump(by_alias=True)
        valuation_history = None
        try:
            val_res = await mcp.call_tool_by_name("get_valuation_timeseries", {"ticker": ticker})
            if hasattr(val_res, "content") and val_res.content:
                val_text = (
                    val_res.content[0].text
                    if hasattr(val_res.content[0], "text")
                    else str(val_res.content[0])
                )
                valuation_history = json.loads(val_text)
        except Exception as e:
            logger.debug("Valuation timeseries fetch failed (non-fatal): %s", e)

        return {
            "fundamentals": fundamentals,
            "_financials_raw": data,
            "valuation_history": valuation_history,
        }
    except Exception as e:
        logger.warning("Fundamentals failed for %s: %s", ticker, e)
        return {"fundamentals": None, "_financials_raw": {}, "valuation_history": None}


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
                "options_signals": OptionsSignals(
                    put_call_oi_ratio=round(put_oi / call_oi, 3) if call_oi > 0 else None,
                    note="No options volume — market may be closed or chain is illiquid",
                ).model_dump(),
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
            "options_signals": OptionsSignals(
                put_call_volume_ratio=pc_vol,
                put_call_oi_ratio=pc_oi,
                call_volume=call_vol,
                put_volume=put_vol,
                total_volume=total_vol,
                flow_signal=flow_signal,
            ).model_dump(),
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
        trade_count = buys + sells
        return {
            "insider_signals": InsiderSignals(
                recent_transaction_count=total,
                buy_signals=buys,
                sell_signals=sells,
                direction=direction,
                net_shares=summary.get("net_shares"),
                net_value=summary.get("net_value"),
                insider_pct_held=insider_pct,
                activity_level="high" if trade_count >= 5 else "moderate" if trade_count >= 2 else "low",
            ).model_dump(),
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

    ticker = state["ticker"]
    mcp = state.get("mcp_client")

    recent_upgrades = 0
    recent_downgrades = 0
    recent_initiations = 0
    latest_actions: list[AnalystAction] = []
    grade_momentum = "stable"

    if mcp:
        try:
            activity_res = await mcp.call_tool_by_name(
                "get_analyst_activity", {"ticker": ticker, "limit": 15}
            )
            if hasattr(activity_res, "content") and activity_res.content:
                act_text = (
                    activity_res.content[0].text
                    if hasattr(activity_res.content[0], "text")
                    else str(activity_res.content[0])
                )
                act_data = json.loads(act_text)
                recent_upgrades = act_data.get("upgrades", 0)
                recent_downgrades = act_data.get("downgrades", 0)
                recent_initiations = act_data.get("initiations", 0)
                latest_actions = [
                    AnalystAction(
                        firm=a.get("firm", ""),
                        action=a.get("action", ""),
                        to_grade=a.get("to_grade", ""),
                        new_target=a.get("new_target"),
                    )
                    for a in (act_data.get("activities") or [])[:5]
                ]
                if recent_upgrades > recent_downgrades * 1.5:
                    grade_momentum = "improving"
                elif recent_downgrades > recent_upgrades * 1.5:
                    grade_momentum = "deteriorating"
        except Exception as e:
            logger.debug("Analyst activity enrichment failed (non-fatal): %s", e)

    eps_revision_up_30d = None
    eps_revision_down_30d = None
    eps_revision_momentum = None
    forward_eps_growth = None

    if mcp:
        try:
            earn_res = await mcp.call_tool_by_name(
                "get_earnings_history", {"ticker": ticker, "limit": 4}
            )
            if hasattr(earn_res, "content") and earn_res.content:
                earn_text = (
                    earn_res.content[0].text
                    if hasattr(earn_res.content[0], "text")
                    else str(earn_res.content[0])
                )
                earn_data = json.loads(earn_text)
                revisions = earn_data.get("eps_revisions", [])
                if revisions:
                    current_q = revisions[0]
                    eps_revision_up_30d = current_q.get("up_last_30d")
                    eps_revision_down_30d = current_q.get("down_last_30d")
                    up = eps_revision_up_30d or 0
                    down = eps_revision_down_30d or 0
                    if up > down * 2:
                        eps_revision_momentum = "positive"
                    elif down > up * 2:
                        eps_revision_momentum = "negative"
                    else:
                        eps_revision_momentum = "flat"
                fwd = earn_data.get("forward_estimates", [])
                if fwd:
                    g = fwd[0].get("growth")
                    forward_eps_growth = g if isinstance(g, (int, float)) else None
        except Exception as e:
            logger.debug("Earnings trend enrichment failed (non-fatal): %s", e)

    return {
        "positioning": AnalystPositioning(
            recommendation_key=rec_key,
            consensus_score=consensus_score,
            n_analysts=n_analysts,
            analyst_target_price=target_mean or None,
            analyst_upside_pct=analyst_upside,
            short_ratio=short_ratio,
            short_pct_float=short_pct_float,
            earnings_surprise_est=earnings_surprise,
            short_squeeze_risk=bool(short_ratio and short_ratio > 5),
            recent_upgrades=recent_upgrades,
            recent_downgrades=recent_downgrades,
            recent_initiations=recent_initiations,
            grade_momentum=grade_momentum,
            latest_actions=latest_actions,
            eps_revision_up_30d=eps_revision_up_30d,
            eps_revision_down_30d=eps_revision_down_30d,
            eps_revision_momentum=eps_revision_momentum,
            forward_eps_growth=forward_eps_growth,
        ).model_dump(),
    }
