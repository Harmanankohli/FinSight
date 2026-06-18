import logging
from datetime import datetime, timedelta

import numpy as np
from scipy import signal

logger = logging.getLogger(__name__)


def _holt_winters(
    series: list[float],
    horizon: int,
    alpha: float = 0.3,
    beta: float = 0.1,
    gamma: float = 0.1,
    period: int = 5,
) -> tuple[list[float], list[float], list[float]]:
    n = len(series)
    if n < 60:
        return [], [], []

    seasonal = [0.0] * n
    if n >= 2 * period:
        for i in range(period):
            s_sum = 0.0
            cnt = 0
            for j in range(i, n, period):
                s_sum += series[j]
                cnt += 1
            seasonal[i] = s_sum / cnt if cnt > 0 else 0
        avg_seasonal = np.mean(seasonal[:period])
        seasonal = [s - avg_seasonal for s in seasonal]
        for i in range(period, n):
            seasonal[i] = seasonal[i % period]
    else:
        period = 1
        seasonal = [0.0] * n

    level = series[0]
    trend = series[1] - series[0] if n > 1 else 0.0
    if n > period:
        seasonals = seasonal[:period]
    else:
        seasonals = [0.0] * period

    residuals = []
    for i in range(n):
        if i < period:
            predicted = level + trend + seasonals[i % period]
        else:
            predicted = level + trend + seasonals[i % period]
        residual = series[i] - predicted
        residuals.append(residual)
        level_new = alpha * (series[i] - seasonals[i % period]) + (1 - alpha) * (level + trend)
        trend = beta * (level_new - level) + (1 - beta) * trend
        seasonals[i % period] = gamma * (series[i] - level) + (1 - gamma) * seasonals[i % period]
        level = level_new

    forecast = []
    lower = []
    upper = []
    last_level = level
    last_trend = trend
    residual_std = np.std(residuals) if len(residuals) > 1 else 0

    for h in range(horizon):
        f = last_level + (h + 1) * last_trend + seasonals[(n + h) % period]
        forecast.append(f)
        band = 1.28 * residual_std * (1 + h * 0.05)
        lower.append(f - band)
        upper.append(f + band)

    return forecast, lower, upper


async def _run_forecast(price_data: dict) -> dict:
    import json

    try:
        sorted_dates = sorted(price_data.keys())
        closes = [price_data[d] for d in sorted_dates if price_data[d] is not None]
        if len(closes) < 60:
            return {
                "method": "exponential_smoothing",
                "horizon_days": 30,
                "forecast_prices": [],
                "forecast_dates": [],
                "confidence_lower": [],
                "confidence_upper": [],
                "mape": None,
            }

        forecast, lower, upper = _holt_winters(closes, 30)

        if not forecast:
            return {
                "method": "exponential_smoothing",
                "horizon_days": 30,
                "forecast_prices": [],
                "forecast_dates": [],
                "confidence_lower": [],
                "confidence_upper": [],
                "mape": None,
            }

        raw_date = sorted_dates[-1].split("T")[0] if "T" in sorted_dates[-1] else sorted_dates[-1]
        last_date = datetime.strptime(raw_date, "%Y-%m-%d") if "-" in raw_date else datetime.now()
        dates = [(last_date + timedelta(days=i + 1)).strftime("%Y-%m-%d") for i in range(30)]

        holdout_size = min(max(1, len(closes) // 5), 30)
        if holdout_size >= 5:
            train = closes[:-holdout_size]
            actual = closes[-holdout_size:]
            f_holdout, _, _ = _holt_winters(train, holdout_size)
            if f_holdout and actual:
                mape = (
                    np.mean([abs((a - f) / a) for a, f in zip(actual, f_holdout) if a != 0]) * 100
                )
            else:
                mape = None
        else:
            mape = None

        logger.info(
            "Forecast complete: horizon=30d mape=%s", f"{mape:.2f}%" if mape is not None else "N/A"
        )
        return {
            "method": "exponential_smoothing",
            "horizon_days": 30,
            "forecast_prices": [round(f, 2) for f in forecast],
            "forecast_dates": dates,
            "confidence_lower": [round(l, 2) for l in lower],
            "confidence_upper": [round(u, 2) for u in upper],
            "mape": round(mape, 2) if mape is not None else None,
        }
    except Exception as e:
        logger.warning("Forecast failed: %s", e)
        return {
            "method": "exponential_smoothing",
            "horizon_days": 30,
            "forecast_prices": [],
            "forecast_dates": [],
            "confidence_lower": [],
            "confidence_upper": [],
            "mape": None,
        }
