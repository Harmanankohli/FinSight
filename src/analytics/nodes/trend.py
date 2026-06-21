"""Trend detection for the analytics agent.

Computes moving average crossovers (SMA20/50/200), MACD, momentum (ROC),
and RSI to produce a composite trend signal.  Uses a scoring system that
weighs each indicator and maps the total to bull/neutral/bear.
"""

import logging

import numpy as np
import pandas as pd

from shared.agent_models import TrendAnalysis
from shared.metrics import compute_rsi_wilder

logger = logging.getLogger(__name__)


def _sma(data: list[float], window: int) -> list[float | None]:
    """Simple moving average via numpy convolution.

    Returns None-prefixed list of same length as input; first
    (window-1) entries are None (not enough data to compute).
    """
    if len(data) < window:
        return [None] * len(data)
    arr = np.array(data, dtype=float)
    sma = np.convolve(arr, np.ones(window), "valid") / window
    return [None] * (window - 1) + sma.tolist()


def _ema(data: list[float], span: int) -> list[float]:
    """Exponential moving average — recursive formulation.

    EMA[i] = (price[i] - EMA[i-1]) * multiplier + EMA[i-1]
    where multiplier = 2 / (span + 1).  Seed value is the first data point.
    """
    multiplier = 2.0 / (span + 1)
    ema = [data[0]]
    for i in range(1, len(data)):
        ema.append((data[i] - ema[-1]) * multiplier + ema[-1])
    return ema


async def _detect_trends(price_data: dict) -> dict:
    """Composite trend analysis: MA crossovers, MACD, momentum, RSI.

    Scoring system (max ~10):
      - Above/below SMA200: +3/0
      - Above/below SMA50: +2/0
      - SMA50 > SMA200: +2/0
      - MACD bullish: +1/0
      - Momentum positive: +1/0
      - RSI > 50: +1/0

    Returns TrendAnalysis model dump or empty defaults on failure.
    """
    try:
        sorted_dates = sorted(price_data.keys())
        closes = [price_data[d] for d in sorted_dates if price_data[d] is not None]
        if len(closes) < 50:
            return TrendAnalysis(
                supporting_indicators=["insufficient_data"],
            ).model_dump()

        sma50 = _sma(closes, 50)
        sma200 = _sma(closes, 200)

        ma_crossover = None
        valid_sma50 = [v for v in sma50 if v is not None]
        valid_sma200 = [v for v in sma200 if v is not None]
        if len(valid_sma50) >= 2 and len(valid_sma200) >= 2:
            prev_sma50 = valid_sma50[-2]
            curr_sma50 = valid_sma50[-1]
            prev_sma200 = valid_sma200[-2]
            curr_sma200 = valid_sma200[-1]
            if curr_sma50 and curr_sma200 and prev_sma50 and prev_sma200:
                if prev_sma50 <= prev_sma200 and curr_sma50 > curr_sma200:
                    ma_crossover = "golden_cross"
                elif prev_sma50 >= prev_sma200 and curr_sma50 < curr_sma200:
                    ma_crossover = "death_cross"

        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26, strict=False)]
        macd_signal_line = _ema(macd_line, 9)
        macd_histogram = [m - s for m, s in zip(macd_line, macd_signal_line, strict=False)]
        macd_bullish = macd_histogram[-1] > 0 if macd_histogram else None

        roc_20d = (closes[-1] - closes[-20]) / closes[-20] if len(closes) >= 20 else 0
        roc_60d = (closes[-1] - closes[-60]) / closes[-60] if len(closes) >= 60 else 0
        momentum_bullish = roc_20d > 0.02

        score = 0
        signals = []

        current_close = closes[-1]
        last_sma200 = sma200[-1] if sma200[-1] is not None else None
        last_sma50 = sma50[-1] if sma50[-1] is not None else None

        if last_sma200 is not None and current_close > last_sma200:
            score += 3
            signals.append("Above SMA200 (+3)")
        else:
            signals.append("Below SMA200 (0)")

        if last_sma50 is not None and current_close > last_sma50:
            score += 2
            signals.append("Above SMA50 (+2)")
        else:
            signals.append("Below SMA50 (0)")

        if last_sma50 is not None and last_sma200 is not None and last_sma50 > last_sma200:
            score += 2
            signals.append("SMA50 > SMA200 (+2)")
        else:
            signals.append("SMA50 <= SMA200 (0)")

        logger.debug(
            "MACD: line=%.4f signal=%.4f histogram=%.4f bullish=%s",
            macd_line[-1] if macd_line else float("nan"),
            macd_signal_line[-1] if macd_signal_line else float("nan"),
            macd_histogram[-1] if macd_histogram else float("nan"),
            macd_bullish,
        )
        if macd_bullish:
            score += 1
            signals.append(f"MACD bullish (+1, hist={macd_histogram[-1]:+.4f})")
        else:
            if macd_histogram:
                signals.append(
                    f"MACD not bullish (0, hist={macd_histogram[-1]:+.4f})"
                )
            else:
                signals.append("MACD not bullish (0)")

        if momentum_bullish:
            score += 1
            signals.append(f"Momentum positive (+1, ROC={roc_20d:+.1%})")
        else:
            signals.append(f"Momentum not positive (0, ROC={roc_20d:+.1%})")

        # RSI via shared compute_rsi_wilder (single source of truth)
        rsi_val = None
        if len(closes) >= 15:
            rsi_val = compute_rsi_wilder(pd.Series(closes), 14)
            if rsi_val > 50:
                score += 1
                signals.append(f"RSI > 50 (+1, RSI={rsi_val:.1f})")
            else:
                signals.append(f"RSI <= 50 (0, RSI={rsi_val:.1f})")

        if score >= 8:
            direction = "bullish"
        elif score >= 5:
            direction = "bullish"
        elif score >= 3:
            direction = "neutral"
        else:
            direction = "bearish"

        strength = min(score / 10.0, 1.0)

        logger.debug(
            "Trend detection: direction=%s strength=%.2f crossover=%s",
            direction,
            strength,
            ma_crossover,
        )
        return TrendAnalysis(
            trend_direction=direction,
            ma_crossover_signal=ma_crossover,
            momentum_shift="accelerating"
            if roc_20d > roc_60d
            else "decelerating"
            if roc_20d < roc_60d
            else None,
            trend_strength=round(strength, 2),
            supporting_indicators=signals if signals else ["neutral"],
        ).model_dump()
    except Exception as e:
        logger.warning("Trend detection failed: %s", e)
        return TrendAnalysis().model_dump()
