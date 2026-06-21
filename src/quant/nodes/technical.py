"""Technical analysis metrics node.

Computes alpha, beta, RSI, MACD, moving averages,
and other price-based indicators.
"""
import logging

import numpy as np
import pandas as pd

from shared.agent_models import QuantRiskMetrics, TechnicalIndicators
from shared.logging_config import logged
from shared.metrics import (
    compute_alpha,
    compute_beta,
    compute_calmar_ratio,
    compute_information_ratio,
    compute_rsi_wilder,
    compute_sharpe_ratio,
    compute_sortino_ratio,
    metric_result,
)

from ..state import QuantAnalysisState
from .calculations import _parse_price_data

logger = logging.getLogger(__name__)

RF_ANNUAL = 0.043


@logged()
async def compute_metrics_node(state: QuantAnalysisState) -> dict:
    # Computes Sharpe, Sortino, Calmar, Alpha, IR, annualized volatility, VaR (95%),
    # max drawdown, and beta vs S&P 500 — all via shared/metrics.py
    prices_dict = state.get("price_data", {})
    ticker = state.get("ticker", "?")
    if not prices_dict:
        logger.warning("Metrics skipped for %s: no price data available", ticker)
        return {
            "volatility": 0.0,
            "is_high_volatility": False,
            "metrics": QuantRiskMetrics(
                sharpe_ratio=metric_result(0.0, "Annualized Excess Return Sharpe", -999, 999),
                annual_volatility=metric_result(0.0, "Annualized Std Daily Returns", 0, 10),
                beta=metric_result(0.0, "Covariance vs ^GSPC (252d min)", -10, 10),
                var_95_daily=metric_result(0.0, "Historical 5th Percentile", -1, 0),
                max_drawdown=metric_result(0.0, "Peak-to-Trough Decline", -1, 0),
                sortino_ratio=metric_result(
                    0.0, "Annualized Downside Deviation Sharpe",
                    -999, 999,
                ),
                calmar_ratio=metric_result(0.0, "CAGR / Max Drawdown", -999, 999),
                alpha=metric_result(0.0, "CAPM Excess Return (annualized)", -10, 10),
                information_ratio=metric_result(
                    0.0, "Annualized Active Return / Tracking Error",
                    -999, 999,
                ),
            ).model_dump(exclude={
                "quant_confidence", "quant_signal",
                "signals", "signal_scores",
            }),
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
            "metrics": QuantRiskMetrics(
                sharpe_ratio=metric_result(0.0, "Annualized Excess Return Sharpe", -999, 999),
                annual_volatility=metric_result(0.0, "Annualized Std Daily Returns", 0, 10),
                beta=metric_result(0.0, "Covariance vs ^GSPC (252d min)", -10, 10),
                var_95_daily=metric_result(0.0, "Historical 5th Percentile", -1, 0),
                max_drawdown=metric_result(0.0, "Peak-to-Trough Decline", -1, 0),
                sortino_ratio=metric_result(
                    0.0, "Annualized Downside Deviation Sharpe",
                    -999, 999,
                ),
                calmar_ratio=metric_result(0.0, "CAGR / Max Drawdown", -999, 999),
                alpha=metric_result(0.0, "CAPM Excess Return (annualized)", -10, 10),
                information_ratio=metric_result(
                    0.0, "Annualized Active Return / Tracking Error",
                    -999, 999,
                ),
            ).model_dump(exclude={
                "quant_confidence", "quant_signal",
                "signals", "signal_scores",
            }),
        }

    returns_arr = returns.values
    annual_vol = float(np.std(returns_arr, ddof=1) * np.sqrt(252))
    sharpe = compute_sharpe_ratio(returns_arr, RF_ANNUAL)

    var_95 = float(np.percentile(returns_arr, 5))

    running_max = prices.expanding().max()
    drawdown = (prices - running_max) / running_max
    max_dd = float(drawdown.min())

    # CAGR for Calmar — use rolling year return as CAGR proxy
    n_year = min(252, len(returns_arr))
    trailing_cagr = (
        float(prices.iloc[-1] / prices.iloc[-n_year] - 1)
        if len(prices) >= n_year
        else 0.0
    )

    try:
        mcp = state.get("mcp_client")
        sp_data = {}
        if mcp:
            sp_period = state.get("period", "5y")
            sp_result = await mcp.call_tool_by_name(
                "get_prices",
                {"ticker": "^GSPC", "period": sp_period, "interval": "1d"},
            )
            sp_data = _parse_price_data(sp_result, "^GSPC")
        if sp_data:
            sp_series = pd.Series({pd.Timestamp(k): v for k, v in sp_data.items()}).sort_index()
            sp_returns = sp_series.pct_change().dropna()
            common = returns.align(sp_returns, join="inner")
            if len(common[0]) > 252:
                common_252 = (common[0].iloc[-252:], common[1].iloc[-252:])
                beta = compute_beta(common_252[0].values, common_252[1].values)
            elif len(common[0]) > 1:
                beta = compute_beta(common[0].values, common[1].values)
            else:
                beta = 1.0
        else:
            beta = 1.0
    except Exception as e:
        logger.warning("Beta calculation failed for %s: %s, defaulting to 1.0", ticker, e)
        beta = 1.0

    is_high = annual_vol > 0.35

    sortino = compute_sortino_ratio(returns_arr, RF_ANNUAL)
    calmar = compute_calmar_ratio(trailing_cagr, max_dd)
    alpha = 0.0
    information_ratio = 0.0
    if sp_data:
        sp_series_loc = pd.Series({pd.Timestamp(k): v for k, v in sp_data.items()}).sort_index()
        sp_ret = sp_series_loc.pct_change().dropna()
        common_ar = returns.align(sp_ret, join="inner")
        if len(common_ar[0]) > 1:
            alpha = compute_alpha(common_ar[0].values, common_ar[1].values, RF_ANNUAL, beta)
            information_ratio = compute_information_ratio(common_ar[0].values, common_ar[1].values)

    result = {
        "volatility": annual_vol,
        "is_high_volatility": is_high,
        "metrics": QuantRiskMetrics(
            sharpe_ratio=metric_result(sharpe, "Annualized Excess Return Sharpe", -999, 999),
            annual_volatility=metric_result(annual_vol, "Annualized Std Daily Returns", 0, 10),
            beta=metric_result(beta, "Covariance vs ^GSPC (252d min)", -10, 10),
            var_95_daily=metric_result(var_95, "Historical 5th Percentile", -1, 0),
            max_drawdown=metric_result(max_dd, "Peak-to-Trough Decline", -1, 0),
            sortino_ratio=metric_result(sortino, "Annualized Downside Deviation Sharpe", -999, 999),
            calmar_ratio=metric_result(calmar, "CAGR / Max Drawdown", -999, 999),
            alpha=metric_result(alpha, "CAPM Excess Return (annualized)", -10, 10),
            information_ratio=metric_result(
                information_ratio, "Annualized Active Return / Tracking Error", -999, 999
            ),
        ).model_dump(exclude={"quant_confidence", "quant_signal", "signals", "signal_scores"}),
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
        sma50_series = prices.rolling(50).mean()
        sma200_series = prices.rolling(200).mean()
        sma50 = float(sma50_series.iloc[-1]) if len(prices) >= 50 else None
        sma200 = float(sma200_series.iloc[-1]) if len(prices) >= 200 else None
        sma50_prev = float(sma50_series.iloc[-2]) if len(prices) >= 51 else None
        sma200_prev = float(sma200_series.iloc[-2]) if len(prices) >= 201 else None

        ema12 = float(prices.ewm(span=12).mean().iloc[-1])
        ema26 = float(prices.ewm(span=26).mean().iloc[-1])
        macd_series = prices.ewm(span=12).mean() - prices.ewm(span=26).mean()
        macd_line = float(macd_series.iloc[-1])
        signal_line = float(macd_series.ewm(span=9).mean().iloc[-1])
        macd_histogram = macd_line - signal_line
        macd_bullish = macd_histogram > 0

        rsi = compute_rsi_wilder(prices, 14)

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
        # True only if SMA50 crossed above SMA200 between prev and current bar (crossover event)
        golden_cross = bool(
            sma50 is not None
            and sma200 is not None
            and sma50_prev is not None
            and sma200_prev is not None
            and sma50_prev <= sma200_prev
            and sma50 > sma200
        )

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
            "technicals": TechnicalIndicators(
                sma_20=round(sma20, 2) if sma20 else None,
                sma_50=round(sma50, 2) if sma50 else None,
                sma_200=round(sma200, 2) if sma200 else None,
                ema_12=round(ema12, 2),
                ema_26=round(ema26, 2),
                macd=round(macd_line, 4),
                macd_signal=round(signal_line, 4),
                macd_histogram=round(macd_histogram, 4),
                macd_bullish=macd_bullish,
                rsi_14=round(rsi, 1),
                bb_upper=round(bb_upper, 2),
                bb_lower=round(bb_lower, 2),
                bb_position=round(bb_position, 3),
                momentum_20d=round(mom_20d, 2) if mom_20d is not None else None,
                momentum_60d=round(mom_60d, 2) if mom_60d is not None else None,
                support=round(support, 2) if support is not None else None,
                resistance=round(resistance, 2) if resistance is not None else None,
                trend=trend,
                golden_cross=golden_cross,
                above_50d_ma=above_50,
                above_200d_ma=above_200,
            ).model_dump(),
        }
    except Exception as e:
        logger.warning("Technical analysis failed for %s: %s", ticker, e)
        return {"technicals": None}
