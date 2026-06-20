"""Financial calculations node for the quant graph. Contains price parsing, CAGR computation, and ratio math."""
import json
import logging

import numpy as np
import pandas as pd

from shared.agent_models import MonteCarloResult
from shared.logging_config import logged_sync

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@logged_sync()
def _parse_price_data(mcp_result, ticker: str) -> dict:
    # Extract {date: close_price} dict from MCP tool response content (handles TextContent wrapping)
    if not hasattr(mcp_result, "content"):
        return {}
    for item in mcp_result.content:
        raw = item.text if hasattr(item, "text") else str(item)
        try:
            data = json.loads(raw)
            records = data.get("data", [])
            return {
                str(r.get("Date", r.get("date", ""))): float(r.get("Close", r.get("close", 0)))
                for r in records
                if r.get("Date") or r.get("date")
            }
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return {}


@logged_sync()
def _run_monte_carlo(
    prices: pd.Series, n_simulations: int = 5000, horizon_days: int = 252
) -> dict | None:
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
    log_rets = rng.normal(mu - 0.5 * sigma**2, sigma, (horizon_days, n_simulations))
    terminal = current * np.exp(log_rets.sum(axis=0))
    pct_chg = (terminal - current) / current

    return MonteCarloResult(
        p10=round(float(np.percentile(terminal, 10)), 2),
        p25=round(float(np.percentile(terminal, 25)), 2),
        p50=round(float(np.percentile(terminal, 50)), 2),
        p75=round(float(np.percentile(terminal, 75)), 2),
        p90=round(float(np.percentile(terminal, 90)), 2),
        prob_profit=round(float((terminal > current).mean()), 3),
        expected_return_pct=round(float(pct_chg.mean() * 100), 1),
        mc_var_95=round(float(np.percentile(pct_chg, 5)), 4),
        current_price=round(current, 2),
        n_simulations=n_simulations,
        horizon_days=horizon_days,
    ).model_dump()


@logged_sync()
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


@logged_sync()
def _get_fcf_from_financials(financials_dict: dict) -> float | None:
    # Sort periods descending so we return the most recent year's positive FCF
    for period_key in sorted(financials_dict.keys(), reverse=True):
        metrics = financials_dict[period_key]
        if "Free Cash Flow" in metrics:
            fcf = float(metrics["Free Cash Flow"])
            logger.debug("FCF check %s: Free Cash Flow=%s", period_key, fcf)
            if fcf > 0:
                return fcf
    for period_key in sorted(financials_dict.keys(), reverse=True):
        metrics = financials_dict[period_key]
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


@logged_sync()
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
    elif vol > 0.35 and vol <= 0.45:
        score -= 0.1
    elif vol > 0.45:
        score -= 0.3
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


@logged_sync()
def _relative_score(value: float, median: float, higher_is_better: bool) -> float:
    """Score a metric relative to its sector median.

    Returns in [-1, 1]:  >1.5× median (bad direction) → -0.7 .. deeply discounted → +1.0
    """
    if median == 0:
        return 0.0
    ratio = value / median
    if higher_is_better:
        # e.g. ROE, margin: higher ratio = better
        if ratio > 2.0:
            return 1.0
        if ratio > 1.5:
            return 0.6
        if ratio > 1.1:
            return 0.2
        if ratio > 0.7:
            return -0.1
        if ratio > 0.4:
            return -0.4
        return -0.7
    else:
        # e.g. PE, EV/EBITDA: lower ratio = cheaper = better
        if ratio < 0.5:
            return 1.0
        if ratio < 0.75:
            return 0.6
        if ratio < 0.95:
            return 0.2
        if ratio < 1.2:
            return -0.1
        if ratio < 1.6:
            return -0.4
        return -0.7


@logged_sync()
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
            if pe < 12:
                scores.append(1.0)
            elif pe < 18:
                scores.append(0.5)
            elif pe < 25:
                scores.append(0.0)
            elif pe < 40:
                scores.append(-0.3)
            else:
                scores.append(-0.7)
        n += 1

    ev_eb = fund.get("ev_to_ebitda")
    if ev_eb and ev_eb > 0:
        if m.get("ev_ebitda") and m["ev_ebitda"] > 0:
            scores.append(_relative_score(ev_eb, m["ev_ebitda"], higher_is_better=False))
        else:
            if ev_eb < 8:
                scores.append(0.7)
            elif ev_eb < 14:
                scores.append(0.2)
            elif ev_eb < 25:
                scores.append(-0.2)
            else:
                scores.append(-0.6)
        n += 1

    return max(-1.0, min(1.0, sum(scores) / n)) if n > 0 else 0.0


@logged_sync()
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
            if roe > 0.25:
                scores.append(1.0)
            elif roe > 0.15:
                scores.append(0.5)
            elif roe > 0.05:
                scores.append(0.1)
            elif roe < 0:
                scores.append(-0.7)
            else:
                scores.append(-0.1)
        n += 1

    op_margin = fund.get("operating_margin")
    if op_margin is not None:
        if m.get("op_margin") is not None:
            scores.append(_relative_score(op_margin, m["op_margin"], higher_is_better=True))
        else:
            if op_margin > 0.25:
                scores.append(0.8)
            elif op_margin > 0.15:
                scores.append(0.4)
            elif op_margin > 0.05:
                scores.append(0.0)
            elif op_margin < 0:
                scores.append(-0.8)
        n += 1

    d_e = fund.get("debt_to_equity")
    if d_e is not None and d_e > 0:
        if m.get("debt_to_equity") is not None and m["debt_to_equity"] > 0:
            # Lower D/E than sector median = better (less leveraged)
            scores.append(_relative_score(d_e, m["debt_to_equity"], higher_is_better=False))
        else:
            if d_e < 0.30:
                scores.append(0.3)
            elif d_e < 0.80:
                scores.append(0.0)
            elif d_e < 1.50:
                scores.append(-0.2)
            else:
                scores.append(-0.5)
        n += 1

    return max(-1.0, min(1.0, sum(scores) / n)) if n > 0 else 0.0


@logged_sync()
def _score_technicals_trend(tech: dict | None) -> float:
    if not tech:
        return 0.0
    trend_scores = {
        "strong_uptrend": 1.0,
        "uptrend": 0.6,
        "recovery": 0.2,
        "sideways": 0.0,
        "downtrend": -0.8,
    }
    score = trend_scores.get(tech.get("trend", ""), 0.0)
    golden = tech.get("golden_cross")
    if golden is True:
        score = min(1.0, score + 0.2)
    elif golden is False:
        score = max(-1.0, score - 0.1)
    return score


@logged_sync()
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


@logged_sync()
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


@logged_sync()
def _score_behavioral(
    options: dict | None, positioning: dict | None, insider: dict | None
) -> float:
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


@logged_sync()
def _weighted_vote(group_scores: dict[str, float]) -> tuple[str, float]:
    """Returns (BUY/HOLD/SELL, confidence 0-1) from group scores weighted by _SIGNAL_WEIGHTS.

    Missing/zero signals have their weights redistributed proportionally
    across present signals so that total weight always sums to 1.0.

    Confidence (per plan §8.6.2): |composite| × (1 − std(present_signals))
    — rewards consensus across signals, penalises conflict.
    """
    present = {k: v for k, v in group_scores.items() if v != 0.0}
    if not present:
        return "HOLD", 0.5
    total_present_weight = sum(_SIGNAL_WEIGHTS.get(k, 0) for k in present)
    if total_present_weight <= 0:
        return "HOLD", 0.5
    scale = 1.0 / total_present_weight
    composite = sum(group_scores[k] * _SIGNAL_WEIGHTS.get(k, 0) * scale for k in present)
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
