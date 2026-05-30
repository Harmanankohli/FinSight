import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _run_monte_carlo(prices: pd.Series, n_simulations: int = 5000, horizon_days: int = 252) -> dict | None:
    """GBM Monte Carlo with Ito-corrected drift. Returns percentile outcomes over horizon_days."""
    returns = prices.pct_change().dropna()
    if len(returns) < 30:
        return None
    mu = float(returns.mean())
    sigma = float(returns.std())
    current = float(prices.iloc[-1])
    if sigma <= 0 or current <= 0:
        return None

    rng = np.random.default_rng(42)
    log_rets = rng.normal(mu - 0.5 * sigma ** 2, sigma, (horizon_days, n_simulations))
    terminal = current * np.exp(log_rets.sum(axis=0))
    pct_chg = (terminal - current) / current

    return {
        "p10": round(float(np.percentile(terminal, 10)), 2),
        "p25": round(float(np.percentile(terminal, 25)), 2),
        "p50": round(float(np.percentile(terminal, 50)), 2),
        "p75": round(float(np.percentile(terminal, 75)), 2),
        "p90": round(float(np.percentile(terminal, 90)), 2),
        "prob_profit": round(float((terminal > current).mean()), 3),
        "expected_return_pct": round(float(pct_chg.mean() * 100), 1),
        "mc_var_95": round(float(np.percentile(pct_chg, 5)), 4),
        "current_price": round(current, 2),
        "n_simulations": n_simulations,
        "horizon_days": horizon_days,
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


# ---------------------------------------------------------------------------
# Weighted signal scoring helpers (§8.6.2)
# ---------------------------------------------------------------------------

_SIGNAL_WEIGHTS = {
    "risk_quality": 0.15,
    "dcf_value": 0.20,
    "fundamental_value": 0.13,
    "fundamental_quality": 0.12,
    "technicals_trend": 0.15,
    "technicals_momentum": 0.10,
    "peer_positioning": 0.10,
    "behavioral": 0.05,
}


def _score_risk_quality(metrics: dict) -> float:
    sharpe = metrics.get("sharpe_ratio") or 0
    vol = metrics.get("annual_volatility") or 0
    score = 0.0
    if sharpe >= 1.5:
        score += 0.5
    elif sharpe >= 1.0:
        score += 0.3
    elif sharpe >= 0.5:
        score += 0.1
    elif sharpe < 0:
        score -= 0.5
    if vol < 0.15:
        score += 0.3
    elif vol < 0.25:
        score += 0.1
    elif vol > 0.45:
        score -= 0.3
    elif vol > 0.35:
        score -= 0.1
    return max(-1.0, min(1.0, score))


def _score_dcf(dcf: dict | None) -> float:
    if not dcf:
        return 0.0
    upside = dcf.get("upside_pct") or 0
    if upside > 50:
        return 1.0
    elif upside > 30:
        return 0.7
    elif upside > 15:
        return 0.4
    elif upside > 0:
        return 0.1
    elif upside > -15:
        return -0.2
    elif upside > -30:
        return -0.5
    return -1.0


def _relative_score(value: float, median: float, higher_is_better: bool) -> float:
    """Score a metric relative to its sector median.

    Returns in [-1, 1]:  >1.5× median (bad direction) → -0.7 .. deeply discounted → +1.0
    """
    if median == 0:
        return 0.0
    ratio = value / median
    if higher_is_better:
        # e.g. ROE, margin: higher ratio = better
        if ratio > 2.0:   return  1.0
        if ratio > 1.5:   return  0.6
        if ratio > 1.1:   return  0.2
        if ratio > 0.7:   return -0.1
        if ratio > 0.4:   return -0.4
        return                   -0.7
    else:
        # e.g. PE, EV/EBITDA: lower ratio = cheaper = better
        if ratio < 0.5:   return  1.0
        if ratio < 0.75:  return  0.6
        if ratio < 0.95:  return  0.2
        if ratio < 1.2:   return -0.1
        if ratio < 1.6:   return -0.4
        return                   -0.7


def _score_fundamental_value(fund: dict | None, medians: dict | None = None) -> float:
    """Score valuation (PE, EV/EBITDA) relative to sector peers when available."""
    if not fund:
        return 0.0
    m = medians or {}
    scores, n = [], 0

    pe = fund.get("trailing_pe")
    if pe and pe > 0:
        if m.get("pe") and m["pe"] > 0:
            scores.append(_relative_score(pe, m["pe"], higher_is_better=False))
        else:
            # Absolute fallback — universal thresholds
            if pe < 12:      scores.append( 1.0)
            elif pe < 18:    scores.append( 0.5)
            elif pe < 25:    scores.append( 0.0)
            elif pe < 40:    scores.append(-0.3)
            else:            scores.append(-0.7)
        n += 1

    ev_eb = fund.get("ev_to_ebitda")
    if ev_eb and ev_eb > 0:
        if m.get("ev_ebitda") and m["ev_ebitda"] > 0:
            scores.append(_relative_score(ev_eb, m["ev_ebitda"], higher_is_better=False))
        else:
            if ev_eb < 8:    scores.append( 0.7)
            elif ev_eb < 14: scores.append( 0.2)
            elif ev_eb < 25: scores.append(-0.2)
            else:            scores.append(-0.6)
        n += 1

    return max(-1.0, min(1.0, sum(scores) / n)) if n > 0 else 0.0


def _score_fundamental_quality(fund: dict | None, medians: dict | None = None) -> float:
    """Score quality (ROE, margin, D/E) relative to sector peers when available."""
    if not fund:
        return 0.0
    m = medians or {}
    scores, n = [], 0

    roe = fund.get("roe")
    if roe is not None:
        if m.get("roe") is not None:
            scores.append(_relative_score(roe, m["roe"], higher_is_better=True))
        else:
            if roe > 0.25:   scores.append( 1.0)
            elif roe > 0.15: scores.append( 0.5)
            elif roe > 0.05: scores.append( 0.1)
            elif roe < 0:    scores.append(-0.7)
            else:            scores.append(-0.1)
        n += 1

    op_margin = fund.get("operating_margin")
    if op_margin is not None:
        if m.get("op_margin") is not None:
            scores.append(_relative_score(op_margin, m["op_margin"], higher_is_better=True))
        else:
            if op_margin > 0.25:   scores.append( 0.8)
            elif op_margin > 0.15: scores.append( 0.4)
            elif op_margin > 0.05: scores.append( 0.0)
            elif op_margin < 0:    scores.append(-0.8)
        n += 1

    d_e = fund.get("debt_to_equity")
    if d_e is not None and d_e > 0:
        if m.get("debt_to_equity") is not None and m["debt_to_equity"] > 0:
            # Lower D/E than sector median = better (less leveraged)
            scores.append(_relative_score(d_e, m["debt_to_equity"], higher_is_better=False))
        else:
            # Absolute fallback — note: utilities/banks carry high D/E normally
            if d_e < 30:     scores.append( 0.3)
            elif d_e < 80:   scores.append( 0.0)
            elif d_e < 150:  scores.append(-0.2)
            else:            scores.append(-0.5)
        n += 1

    return max(-1.0, min(1.0, sum(scores) / n)) if n > 0 else 0.0


def _score_technicals_trend(tech: dict | None) -> float:
    if not tech:
        return 0.0
    trend_scores = {
        "strong_uptrend": 1.0, "uptrend": 0.6,
        "recovery": 0.2, "sideways": 0.0, "downtrend": -0.8,
    }
    score = trend_scores.get(tech.get("trend", ""), 0.0)
    golden = tech.get("golden_cross")
    if golden is True:
        score = min(1.0, score + 0.2)
    elif golden is False:
        score = max(-1.0, score - 0.1)
    return score


def _score_technicals_momentum(tech: dict | None) -> float:
    if not tech:
        return 0.0
    scores, n = [], 0
    rsi = tech.get("rsi_14")
    if rsi is not None:
        if rsi < 30:
            scores.append(0.8)
        elif rsi < 45:
            scores.append(0.3)
        elif rsi < 60:
            scores.append(0.0)
        elif rsi < 75:
            scores.append(-0.2)
        else:
            scores.append(-0.6)
        n += 1
    macd_bull = tech.get("macd_bullish")
    if macd_bull is not None:
        scores.append(0.5 if macd_bull else -0.4)
        n += 1
    return max(-1.0, min(1.0, sum(scores) / n)) if n > 0 else 0.0


def _score_peer_positioning(peer_comp: dict | None) -> float:
    if not peer_comp or not peer_comp.get("rankings"):
        return 0.0
    rankings = peer_comp["rankings"]
    n_peers = peer_comp.get("n_peers", 1)
    if not rankings or n_peers == 0:
        return 0.0
    total = n_peers + 1  # includes self
    avg_rank = sum(rankings.values()) / len(rankings)
    # rank 1 → +1.0, rank total → -1.0
    normalized = 1.0 - 2.0 * (avg_rank - 1) / max(1, total - 1)
    return max(-1.0, min(1.0, normalized))


def _score_behavioral(options: dict | None, positioning: dict | None, insider: dict | None) -> float:
    scores, n = [], 0
    if options:
        pc = options.get("put_call_volume_ratio")
        if pc is not None:
            if pc < 0.5:
                scores.append(0.7)
            elif pc < 0.8:
                scores.append(0.3)
            elif pc < 1.2:
                scores.append(0.0)
            elif pc < 1.8:
                scores.append(-0.4)
            else:
                scores.append(-0.8)
            n += 1
    if positioning:
        cs = positioning.get("consensus_score")
        if cs is not None:
            scores.append(cs / 2.0)  # scale -2..+2 → -1..+1
            n += 1
        au = positioning.get("analyst_upside_pct")
        if au is not None:
            if au > 20:
                scores.append(0.5)
            elif au > 10:
                scores.append(0.2)
            elif au < -10:
                scores.append(-0.5)
            elif au < 0:
                scores.append(-0.2)
            else:
                scores.append(0.0)
            n += 1
    if insider:
        direction = insider.get("direction", "neutral")
        if direction == "net_buy":
            scores.append(0.6)
            n += 1
        elif direction == "net_sell":
            scores.append(-0.4)
            n += 1
    return max(-1.0, min(1.0, sum(scores) / n)) if n > 0 else 0.0


def _weighted_vote(group_scores: dict[str, float]) -> tuple[str, float]:
    """Returns (BUY/HOLD/SELL, confidence 0-1) from group scores weighted by _SIGNAL_WEIGHTS.

    Missing/zero signals contribute zero — they neither push nor normalize.
    A single mild positive in one of 8 groups should not become a high-confidence BUY.

    Confidence (per plan §8.6.2): |composite| × (1 − std(present_signals))
    — rewards consensus across signals, penalises conflict.
    """
    present = {k: v for k, v in group_scores.items() if v != 0.0}
    if not present:
        return "HOLD", 0.5
    composite = sum(group_scores[k] * _SIGNAL_WEIGHTS.get(k, 0) for k in present)
    if composite > 0.15:
        rec = "BUY"
    elif composite < -0.15:
        rec = "SELL"
    else:
        rec = "HOLD"
    if len(present) > 1:
        vals = list(present.values())
        mean = sum(vals) / len(vals)
        std = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
        confidence = abs(composite) * max(0.0, 1.0 - std)
    else:
        confidence = abs(composite)
    return rec, round(min(1.0, confidence), 3)


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

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
    """Beta-adjusted historical crash scenarios + GBM Monte Carlo simulation."""
    prices_dict = state.get("price_data", {})
    ticker = state.get("ticker", "?")
    if not prices_dict:
        logger.warning("Stress test skipped for %s: no price data", ticker)
        return {"stress_test_result": None, "monte_carlo": None}

    prices = pd.Series(
        {pd.Timestamp(k): v for k, v in prices_dict.items()}
    ).sort_index()
    returns = prices.pct_change().dropna()
    current_price = float(prices.iloc[-1])
    beta = (state.get("metrics") or {}).get("beta") or 1.0

    # Fetch live historical shock percentages for the ticker's sector.
    # Falls back to hardcoded S&P values if MCP is unavailable.
    mcp = state.get("mcp_client")
    sector = (state.get("fundamentals") or {}).get("sector", "") or ""
    market_scenarios: dict[str, float] = {
        "market_crash_2008": -0.565,
        "covid_crash_2020":  -0.340,
        "dot_com_bubble":    -0.491,
        "mild_recession":    -0.254,
    }
    if mcp:
        try:
            r = await mcp.call_tool_by_name("get_scenario_shocks", {"sector": sector})
            if hasattr(r, "content") and r.content:
                raw = r.content[0].text if hasattr(r.content[0], "text") else str(r.content[0])
                live = json.loads(raw).get("shocks", {})
                if live:
                    market_scenarios.update(live)
                    logger.info(
                        "Live scenario shocks for %s (sector=%s, index=%s): %s",
                        ticker, sector, json.loads(raw).get("index_used"), live,
                    )
        except Exception as _se:
            logger.warning("get_scenario_shocks failed for %s: %s — using fallback", ticker, _se)

    results = {}
    for scenario, mkt_decline in market_scenarios.items():
        adj_decline = mkt_decline * beta
        projected = current_price * (1 + adj_decline)
        results[scenario] = {
            "market_decline_pct": round(mkt_decline, 4),
            "beta_adj_decline_pct": round(adj_decline, 4),
            "projected_price": round(projected, 2),
            "loss_per_share": round(projected - current_price, 2),
        }

    var_95 = float(np.percentile(returns, 5))
    cvar_95 = float(returns[returns <= var_95].mean()) if len(returns[returns <= var_95]) > 0 else var_95

    mc = _run_monte_carlo(prices)

    return {
        "stress_test_result": {
            "scenarios": results,
            "var_95": round(var_95, 4),
            "cvar_95": round(cvar_95, 4),
            "beta_used": round(beta, 3),
            "beta_adjusted": True,
        },
        "monte_carlo": mc,
    }


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


async def peer_comparison_node(state: QuantAnalysisState) -> dict:
    """Fetches peer financials in parallel and ranks the primary ticker on 5 metrics."""
    ticker = state["ticker"]
    mcp = state.get("mcp_client")
    raw = state.get("_financials_raw") or {}
    if not mcp:
        return {"peer_comparison": None}

    info = raw.get("info", {}) if raw else {}
    industry = info.get("industry", "")
    sector = info.get("sector", "")

    # Discover peers dynamically via Yahoo Finance recommendations API;
    # fall back to curated sets when the API returns empty (cold MCP server, rate limit, etc.)
    peer_tickers = []
    try:
        pr = await mcp.call_tool_by_name("get_peers", {"ticker": ticker})
        if hasattr(pr, "content") and pr.content:
            raw_text = pr.content[0].text if hasattr(pr.content[0], "text") else str(pr.content[0])
            peer_tickers = json.loads(raw_text).get("peers", [])
    except Exception as _pe:
        logger.warning("get_peers failed for %s: %s", ticker, _pe)

    if not peer_tickers:
        return {"peer_comparison": {
            "note": f"Peer discovery unavailable for {ticker} — get_peers returned no results. Restart MCP server if recently deployed.",
            "industry": industry,
            "sector": sector,
        }}

    async def _fetch(sym: str) -> tuple[str, dict]:
        try:
            r = await mcp.call_tool_by_name("get_financials", {"ticker": sym})
            if hasattr(r, "content") and r.content:
                txt = r.content[0].text if hasattr(r.content[0], "text") else str(r.content[0])
                d = json.loads(txt)
                return sym, d.get("info", {})
        except Exception as e:
            logger.debug("Peer %s financials failed: %s", sym, e)
        return sym, {}

    # Limit to 3 concurrent get_financials calls so the MCP/yfinance rate limiter
    # doesn't queue more requests than the per-attempt timeout can absorb.
    _sem = asyncio.Semaphore(3)
    async def _fetch_capped(sym: str) -> tuple[str, dict]:
        async with _sem:
            return await _fetch(sym)
    peer_infos = dict(await asyncio.gather(*[_fetch_capped(p) for p in peer_tickers]))

    def _extract(inf: dict) -> dict:
        return {
            "pe": inf.get("trailingPE"),
            "ev_ebitda": inf.get("enterpriseToEbitda"),
            "rev_growth": inf.get("revenueGrowth"),
            "op_margin": inf.get("operatingMargins"),
            "roe": inf.get("returnOnEquity"),
            "debt_to_equity": inf.get("debtToEquity"),
            "market_cap": inf.get("marketCap"),
        }

    comparison: dict[str, dict] = {ticker: _extract(info)}
    for sym, sinf in peer_infos.items():
        comparison[sym] = _extract(sinf)

    # Rank primary ticker (1 = best) on each metric
    rankings: dict[str, int] = {}
    metric_higher_better = {
        "pe": False, "ev_ebitda": False,
        "rev_growth": True, "op_margin": True, "roe": True,
    }
    for metric, hib in metric_higher_better.items():
        vals = {t: v[metric] for t, v in comparison.items() if v.get(metric) is not None and v[metric] > 0}
        if ticker not in vals or len(vals) < 2:
            continue
        ordered = sorted(vals.keys(), key=lambda t: vals[t], reverse=hib)
        rankings[metric] = ordered.index(ticker) + 1

    # Compute sector medians so scoring functions can do relative comparisons
    # instead of relying on absolute universal thresholds.
    medians: dict[str, float] = {}
    for metric in ("pe", "ev_ebitda", "rev_growth", "op_margin", "roe", "debt_to_equity"):
        vals_list = sorted(
            v[metric] for v in comparison.values()
            if v.get(metric) is not None and isinstance(v[metric], (int, float))
            # allow negative values for quality metrics; skip negative for valuation ratios
            and (metric not in ("pe", "ev_ebitda") or v[metric] > 0)
        )
        if vals_list:
            mid = len(vals_list) // 2
            medians[metric] = vals_list[mid] if len(vals_list) % 2 else (vals_list[mid - 1] + vals_list[mid]) / 2

    return {
        "peer_comparison": {
            "industry": industry,
            "sector": sector,
            "peers": peer_tickers,
            "comparison": comparison,
            "rankings": rankings,
            "n_peers": len(peer_tickers),
            "medians": medians,
        }
    }


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
        raw = result.content[0].text if hasattr(result.content[0], "text") else str(result.content[0])
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


async def insider_signals_node(state: QuantAnalysisState) -> dict:
    """Fetches insider buy/sell transactions via yfinance (structured data, not keyword matching)."""
    ticker = state["ticker"]
    mcp = state.get("mcp_client")
    if not mcp:
        return {"insider_signals": None}
    try:
        result = await mcp.call_tool_by_name("get_insider_transactions", {"ticker": ticker, "days": 90})
        if not hasattr(result, "content") or not result.content:
            return {"insider_signals": None}
        raw = result.content[0].text if hasattr(result.content[0], "text") else str(result.content[0])
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


async def analyst_positioning_node(state: QuantAnalysisState) -> dict:
    """Extracts analyst consensus, price target upside, and short interest from pre-fetched financials."""
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
        "strongbuy": 2, "strong_buy": 2,
        "buy": 1,
        "hold": 0, "neutral": 0,
        "underperform": -1,
        "sell": -2, "strongsell": -2, "strong_sell": -2,
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
    """Weighted 8-group signal voting → BUY/HOLD/SELL with composite score."""
    metrics = state.get("metrics", {})
    stress = state.get("stress_test_result")
    dcf = state.get("dcf_valuation")
    corr = state.get("correlation_matrix", {})
    fundamentals = state.get("fundamentals")
    technicals = state.get("technicals")
    peer_comp = state.get("peer_comparison")
    options = state.get("options_signals")
    insider = state.get("insider_signals")
    positioning = state.get("positioning")
    ticker = state.get("ticker", "?")
    dcf_error = state.get("dcf_error")

    logger.info(
        "Formatting output for %s: dcf=%s, stress=%s, fundamentals=%s, technicals=%s, peers=%s",
        ticker,
        "ok" if dcf else "null",
        "ok" if stress else "null",
        "ok" if fundamentals else "null",
        "ok" if technicals else "null",
        "ok" if peer_comp and peer_comp.get("rankings") else "null",
    )

    # Sector medians from peer_comparison_node for relative scoring
    peer_medians = (peer_comp or {}).get("medians") if peer_comp else None

    # Compute per-group scores
    group_scores = {
        "risk_quality": _score_risk_quality(metrics),
        "dcf_value": _score_dcf(dcf),
        "fundamental_value": _score_fundamental_value(fundamentals, peer_medians),
        "fundamental_quality": _score_fundamental_quality(fundamentals, peer_medians),
        "technicals_trend": _score_technicals_trend(technicals),
        "technicals_momentum": _score_technicals_momentum(technicals),
        "peer_positioning": _score_peer_positioning(peer_comp),
        "behavioral": _score_behavioral(options, positioning, insider),
    }

    recommendation, confidence = _weighted_vote(group_scores)

    # Named signal list (for display/backward compat)
    signals: list[str] = []
    sharpe = metrics.get("sharpe_ratio", 0)
    vol = metrics.get("annual_volatility", 0)
    if sharpe >= 1.0:
        signals.append("positive_risk_adjusted_return")
    elif sharpe < 0:
        signals.append("negative_risk_adjusted_return")
    if vol > 0.35:
        signals.append("high_volatility")
    elif vol < 0.15:
        signals.append("low_volatility")
    if dcf:
        if dcf.get("upside_pct", 0) > 20:
            signals.append("undervalued_dcf")
        elif dcf.get("upside_pct", 0) < -20:
            signals.append("overvalued_dcf")
    if stress and stress.get("cvar_95", 0) < -0.05:
        signals.append("tail_risk")
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
    if peer_comp and peer_comp.get("rankings"):
        avg_rank = sum(peer_comp["rankings"].values()) / len(peer_comp["rankings"])
        n_peers = peer_comp.get("n_peers", 1)
        if avg_rank <= 2:
            signals.append("top_peer_rank")
        elif avg_rank > n_peers:
            signals.append("bottom_peer_rank")
    if options:
        if options.get("flow_signal") == "bullish":
            signals.append("bullish_options_flow")
        elif options.get("flow_signal") == "bearish":
            signals.append("bearish_options_flow")
    if positioning:
        if (positioning.get("consensus_score") or 0) >= 1:
            signals.append("analyst_buy_consensus")
        elif (positioning.get("consensus_score") or 0) <= -1:
            signals.append("analyst_sell_consensus")

    # Build reasoning string
    parts = []
    if metrics:
        parts.append(
            f"Sharpe: {metrics.get('sharpe_ratio', 'N/A')}, "
            f"Vol: {metrics.get('annual_volatility', 'N/A')}, "
            f"Beta: {metrics.get('beta', 'N/A')}"
        )
    if dcf:
        parts.append(
            f"DCF intrinsic: ${dcf.get('intrinsic_value', 'N/A')} "
            f"(upside: {dcf.get('upside_pct', 'N/A')}%, "
            f"WACC: {dcf.get('wacc', 'N/A'):.1%}, "
            f"growth: {dcf.get('growth_rate', 'N/A'):.1%})"
        )
    elif dcf_error:
        parts.append(f"DCF: {dcf_error}")
    if fundamentals:
        fund_parts = []
        for label, key, fmt in [
            ("PE", "trailing_pe", ".1f"),
            ("ROE", "roe", ".1%"),
            ("RevGrowth", "revenue_growth", ".1%"),
            ("OpMargin", "operating_margin", ".1%"),
            ("D/E", "debt_to_equity", ".1f"),
        ]:
            v = fundamentals.get(key)
            if v is not None:
                fund_parts.append(f"{label}={v:{fmt}}")
        if fund_parts:
            parts.append(f"Fundamentals: {', '.join(fund_parts)}")
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
            parts.append(f"Technicals: {', '.join(tech_parts)}")
    if peer_comp and peer_comp.get("rankings"):
        ranks = peer_comp["rankings"]
        parts.append(f"Peers: rank {ranks} among {peer_comp.get('n_peers', '?')+1}")
    if stress:
        parts.append(f"Stress CVaR: {stress.get('cvar_95', 'N/A')}")
    parts.append(
        f"Composite: {sum(group_scores[k]*_SIGNAL_WEIGHTS.get(k,0) for k in group_scores):.3f} "
        f"({recommendation}, conf={confidence:.2f})"
    )

    stress_test_info = stress or (
        {"note": f"Stress test skipped - volatility ({vol:.1%}) below 35% threshold", "volatility": vol, "threshold": 0.35}
        if vol <= 0.35 else None
    )

    return {
        "recommendation": recommendation,
        "reasoning": " | ".join(parts),
        "metrics": {
            **metrics,
            "quant_confidence": confidence,
            "quant_signal": recommendation,
            "signals": signals,
            "signal_scores": group_scores,
        },
        "stress_test_result": stress_test_info,
    }


async def llm_summary_node(state: QuantAnalysisState) -> dict:
    """Produces a 3-4 sentence investor summary covering all signal groups."""
    from langchain_openai import ChatOpenAI
    from shared.config import LLM_SUMMARY_MODEL as LLM_MODEL, LLM_BASE_URL, LLM_API_KEY

    metrics = state.get("metrics", {})
    stress = state.get("stress_test_result")
    dcf = state.get("dcf_valuation")
    mc = state.get("monte_carlo")
    fund = state.get("fundamentals") or {}
    tech = state.get("technicals") or {}
    peer_comp = state.get("peer_comparison") or {}
    positioning = state.get("positioning") or {}
    options = state.get("options_signals") or {}
    ticker = state.get("ticker", "")
    rec = state.get("recommendation", "HOLD")
    reasoning = state.get("reasoning", "")

    today = date.today().isoformat()
    prompt = (
        f"Today: {today}. Financial analyst summary for {ticker}.\n"
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
    if mc:
        prompt += (
            f"Monte Carlo (1yr): p10=${mc.get('p10')}, p50=${mc.get('p50')}, "
            f"p90=${mc.get('p90')}, prob_profit={mc.get('prob_profit'):.0%}\n"
        )
    if stress:
        prompt += f"Stress CVaR: {stress.get('cvar_95')}\n"
    if peer_comp.get("rankings"):
        prompt += f"Peer ranks: {peer_comp['rankings']} out of {peer_comp.get('n_peers', '?')+1}\n"
    if positioning.get("recommendation_key"):
        prompt += (
            f"Analyst consensus: {positioning['recommendation_key']} "
            f"({positioning.get('n_analysts', '?')} analysts, "
            f"target upside {positioning.get('analyst_upside_pct')}%)\n"
        )
    if options.get("flow_signal"):
        prompt += f"Options flow: {options['flow_signal']} (P/C vol={options.get('put_call_volume_ratio')})\n"
    prompt += "\nWrite 3-4 sentences for an investor. Note signal conflicts. Be specific about numbers."

    try:
        llm = ChatOpenAI(model=LLM_MODEL, base_url=LLM_BASE_URL, api_key=LLM_API_KEY, temperature=0.3, max_tokens=512)
        response = await llm.ainvoke(prompt)
        summary = response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        logger.warning("LLM summary failed: %s", e)
        summary = reasoning

    return {"reasoning": summary}
