import logging

import pandas as pd

from shared.logging_config import logged

from ..state import QuantAnalysisState
from .calculations import _parse_price_data

logger = logging.getLogger(__name__)


@logged()
async def correlation_node(state: QuantAnalysisState) -> dict:
    # Pairwise Pearson correlation matrix between primary ticker and portfolio holdings
    prices_dict = state.get("price_data", {})
    holdings = state.get("portfolio_holdings", [])
    mcp = state.get("mcp_client")

    if not prices_dict or not holdings or not mcp:
        return {
            "correlation_matrix": {
                "note": "No portfolio holdings provided. Include holdings like 'My portfolio holds AAPL, MSFT' to see correlation analysis."  # noqa: E501
            }
        }

    ticker = state["ticker"]
    all_tickers = [ticker] + holdings

    try:
        all_prices = {ticker: prices_dict}
        for h in holdings:
            result = await mcp.call_tool_by_name(
                "get_prices", {"ticker": h, "period": state.get("period", "5y"), "interval": "1d"}
            )
            hp = _parse_price_data(result, h)
            if hp:
                all_prices[h] = hp

        close_df = pd.DataFrame(
            {
                t: pd.Series({pd.Timestamp(k): v for k, v in p.items()})
                for t, p in all_prices.items()
            }
        ).sort_index()
        returns = close_df.pct_change().dropna()
        if returns.empty or len(returns.columns) < 2:
            return {
                "correlation_matrix": {
                    "note": "Insufficient overlapping price data to compute correlations between holdings."  # noqa: E501
                }
            }
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
