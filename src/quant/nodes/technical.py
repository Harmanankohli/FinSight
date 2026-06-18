import logging

import numpy as np
import pandas as pd

from shared.logging_config import logged

from ..state import QuantAnalysisState
from .calculations import _parse_price_data

logger = logging.getLogger(__name__)


@logged()
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

    prices = pd.Series({pd.Timestamp(k): v for k, v in prices_dict.items()}).sort_index()
    returns = prices.pct_change().dropna()

    if len(returns) < 2:
        logger.warning(
            "Metrics skipped for %s: only %d data points (need >= 2)", ticker, len(returns)
        )
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
    rf_daily = 0.043 / 252
    sharpe = (
        float(((returns.mean() - rf_daily) / returns.std()) * np.sqrt(252))
        if returns.std() > 0
        else 0.0
    )
    var_95 = float(np.percentile(returns, 5))

    running_max = prices.expanding().max()
    drawdown = (prices - running_max) / running_max
    max_dd = float(drawdown.min())

    try:
        mcp = state.get("mcp_client")
        sp_data = {}
        if mcp:
            sp_result = await mcp.call_tool_by_name(
                "get_prices",
                {"ticker": "^GSPC", "period": state.get("period", "5y"), "interval": "1d"},
            )
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


@logged()
async def technical_analysis_node(state: QuantAnalysisState) -> dict:
    """Computes RSI, MACD, Bollinger, SMAs, EMAs, momentum, support/resistance from price_data."""
    prices_dict = state.get("price_data", {})
    ticker = state.get("ticker", "?")
    if not prices_dict:
        return {"technicals": None}
    try:
        prices = pd.Series({pd.Timestamp(k): v for k, v in prices_dict.items()}).sort_index()
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
        rsi = (
            float(rsi_raw.iloc[-1])
            if not rsi_raw.empty and not np.isnan(rsi_raw.iloc[-1])
            else 50.0
        )

        bb_mid = prices.rolling(20).mean()
        bb_std = prices.rolling(20).std()
        bb_upper = float((bb_mid + 2 * bb_std).iloc[-1])
        bb_lower = float((bb_mid - 2 * bb_std).iloc[-1])
        bb_position = (
            (current - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5
        )

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
