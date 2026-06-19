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

        macd_line_list = macd_line
        if len(macd_line_list) >= 2 and macd_line_list[-1] > macd_line_list[-2]:
            score += 1
            signals.append("MACD bullish (+1)")
        else:
            signals.append("MACD not bullish (0)")

        if momentum_bullish:
            score += 1
            signals.append(f"Momentum positive (+1, ROC={roc_20d:+.1%})")
        else:
            signals.append(f"Momentum not positive (0, ROC={roc_20d:+.1%})")

        # RSI > 50 check
        delta_arr = np.diff(closes[-15:]) if len(closes) >= 15 else np.array([])
        if len(delta_arr) > 0:
            gains = np.where(delta_arr > 0, delta_arr, 0)
            losses_arr = np.where(delta_arr < 0, -delta_arr, 0)
            avg_g = float(np.mean(gains))
            avg_l = float(np.mean(losses_arr))
            rsi_val = 100 - (100 / (1 + avg_g / avg_l)) if avg_l > 0 else 100.0
            if rsi_val > 50:
                score += 1
                signals.append(f"RSI > 50 (+1, RSI={rsi_val:.1f})")
            else:
                signals.append(f"RSI <= 50 (0, RSI={rsi_val:.1f})")

        if score >= 8:
            direction = "strong_bullish"
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
        return {
            "trend_direction": direction,
            "ma_crossover_signal": ma_crossover,
            "momentum_shift": "accelerating"
            if roc_20d > roc_60d
            else "decelerating"
            if roc_20d < roc_60d
            else None,
            "trend_strength": round(strength, 2),
            "supporting_indicators": signals if signals else ["neutral"],
        }
    except Exception as e:
        logger.warning("Trend detection failed: %s", e)
        return {
            "trend_direction": "neutral",
            "ma_crossover_signal": None,
            "momentum_shift": None,
            "trend_strength": 0.0,
            "supporting_indicators": [],
        }
