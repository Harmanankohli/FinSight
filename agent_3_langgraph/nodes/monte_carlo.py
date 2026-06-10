import json
import logging

import numpy as np
import pandas as pd

from shared.logging_config import logged

from ..state import QuantAnalysisState
from .calculations import _run_monte_carlo

logger = logging.getLogger(__name__)


@logged()
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
