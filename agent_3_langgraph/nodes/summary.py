import asyncio
import json
import logging
from datetime import date

from shared.logging_config import logged
from shared.settings import LLM_SUMMARY_MODEL, LLM_BASE_URL, LLM_API_KEY
from shared.llm_queue import llm_queue, Priority

from ..state import QuantAnalysisState
from .calculations import (
    _SIGNAL_WEIGHTS,
    _score_behavioral,
    _score_dcf,
    _score_fundamental_quality,
    _score_fundamental_value,
    _score_peer_positioning,
    _score_risk_quality,
    _score_technicals_momentum,
    _score_technicals_trend,
    _weighted_vote,
)

logger = logging.getLogger(__name__)


@logged()
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


@logged()
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


@logged()
async def llm_summary_node(state: QuantAnalysisState) -> dict:
    """Produces a 3-4 sentence investor summary covering all signal groups."""
    from langchain_openai import ChatOpenAI

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
        llm = ChatOpenAI(model=LLM_SUMMARY_MODEL, base_url=LLM_BASE_URL, api_key=LLM_API_KEY, temperature=0.3, max_tokens=512)
        async with llm_queue.acquire(Priority.CRITICAL, "quant-summary"):
            response = await llm.ainvoke(prompt)
        summary = response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        logger.warning("LLM summary failed: %s", e)
        summary = reasoning

    return {"reasoning": summary}
