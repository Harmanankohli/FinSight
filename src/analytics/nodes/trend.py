import logging

import numpy as np

logger = logging.getLogger(__name__)


def _sma(data: list[float], window: int) -> list[float | None]:
    if len(data) < window:
        return [None] * len(data)
    arr = np.array(data, dtype=float)
    sma = np.convolve(arr, np.ones(window), "valid") / window
    return [None] * (window - 1) + sma.tolist()


def _ema(data: list[float], span: int) -> list[float]:
    arr = np.array(data, dtype=float)
    multiplier = 2.0 / (span + 1)
    ema = [arr[0]]
    for i in range(1, len(arr)):
        ema.append((arr[i] - ema[-1]) * multiplier + ema[-1])
    return ema


async def _detect_trends(price_data: dict) -> dict:
    try:
        sorted_dates = sorted(price_data.keys())
        closes = [price_data[d] for d in sorted_dates if price_data[d] is not None]
        if len(closes) < 50:
            return {
                "trend_direction": "neutral",
                "ma_crossover_signal": None,
                "momentum_shift": None,
                "trend_strength": 0.0,
                "supporting_indicators": ["insufficient_data"],
            }

        sma20 = _sma(closes, 20)
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
        macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        macd_bullish = macd_line[-1] > macd_line[-2] if len(macd_line) >= 2 else None

        roc_20d = (closes[-1] - closes[-20]) / closes[-20] if len(closes) >= 20 else 0
        roc_60d = (closes[-1] - closes[-60]) / closes[-60] if len(closes) >= 60 else 0
        momentum_bullish = roc_20d > 0.02

        signals = []
        if ma_crossover == "golden_cross":
            signals.append("bullish")
        elif ma_crossover == "death_cross":
            signals.append("bearish")
        if macd_bullish:
            signals.append("bullish")
        elif macd_bullish is False:
            signals.append("bearish")
        if momentum_bullish:
            signals.append("bullish")
        elif roc_20d < -0.02:
            signals.append("bearish")

        bullish_count = signals.count("bullish")
        bearish_count = signals.count("bearish")
        total = len(signals)
        if total == 0:
            direction = "neutral"
            strength = 0.0
        else:
            if bullish_count > bearish_count:
                direction = "bullish"
            elif bearish_count > bullish_count:
                direction = "bearish"
            else:
                direction = "neutral"
            strength = max(bullish_count, bearish_count) / max(total, 1)

        logger.debug("Trend detection: direction=%s strength=%.2f crossover=%s", direction, strength, ma_crossover)
        return {
            "trend_direction": direction,
            "ma_crossover_signal": ma_crossover,
            "momentum_shift": "accelerating" if roc_20d > roc_60d else "decelerating" if roc_20d < roc_60d else None,
            "trend_strength": round(strength, 2),
            "supporting_indicators": signals if signals else ["neutral"],
        }
    except Exception as e:
        logger.warning("Trend detection failed: %s", e)
        return {"trend_direction": "neutral", "ma_crossover_signal": None, "momentum_shift": None, "trend_strength": 0.0, "supporting_indicators": []}
