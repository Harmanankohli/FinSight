import logging

logger = logging.getLogger(__name__)


def _sma(values: list[float], window: int) -> list[float | None]:
    if len(values) < window:
        return [None] * len(values)
    result = []
    for i in range(len(values)):
        if i < window - 1:
            result.append(None)
        else:
            result.append(round(sum(values[i - window + 1 : i + 1]) / window, 2))
    return result


async def _generate_charts(ohlcv_data: list[dict], price_data: dict) -> list[dict]:
    try:
        charts = []

        sorted_dates = sorted(price_data.keys())
        closes = [price_data[d] for d in sorted_dates if price_data[d] is not None]
        dates = sorted_dates[: len(closes)]

        candlestick = {
            "chart_type": "candlestick",
            "labels": [d["date"] for d in ohlcv_data[-90:]],
            "datasets": [
                {
                    "label": "OHLCV",
                    "data": [
                        {
                            "date": d["date"],
                            "open": d["open"],
                            "high": d["high"],
                            "low": d["low"],
                            "close": d["close"],
                            "volume": d["volume"],
                        }
                        for d in ohlcv_data[-90:]
                    ],
                }
            ],
            "annotations": [],
        }
        charts.append(candlestick)

        sma20 = _sma(closes, 20)
        sma50 = _sma(closes, 50)
        sma200 = _sma(closes, 200)
        line_chart = {
            "chart_type": "line",
            "labels": dates,
            "datasets": [
                {"label": "Close", "data": [round(c, 2) for c in closes]},
                {
                    "label": "SMA-20",
                    "data": [round(v, 2) if v is not None else None for v in sma20],
                },
                {
                    "label": "SMA-50",
                    "data": [round(v, 2) if v is not None else None for v in sma50],
                },
                {
                    "label": "SMA-200",
                    "data": [round(v, 2) if v is not None else None for v in sma200],
                },
            ],
            "annotations": [],
        }
        charts.append(line_chart)

        logger.debug("Chart generation complete: %d charts", len(charts))
        return charts
    except Exception as e:
        logger.warning("Chart generation failed: %s", e)
        return []
