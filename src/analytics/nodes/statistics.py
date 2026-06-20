import logging

import numpy as np
from shared.agent_models import StatisticalSummary
from scipy import stats as scipy_stats

logger = logging.getLogger(__name__)


async def _compute_statistics(price_data: dict, mcp_client) -> dict:
    try:
        sorted_dates = sorted(price_data.keys())
        closes = [price_data[d] for d in sorted_dates if price_data[d] is not None]
        if len(closes) < 10:
            return StatisticalSummary().model_dump()

        prices = np.array(closes, dtype=float)
        log_returns = np.diff(np.log(prices))
        if len(log_returns) < 5:
            return StatisticalSummary().model_dump()

        skew = float(scipy_stats.skew(log_returns))
        kurt = float(scipy_stats.kurtosis(log_returns, fisher=True))
        jb_stat, jb_p = scipy_stats.jarque_bera(log_returns)

        if kurt > 3:
            dist_class = "leptokurtic"
        elif kurt < 3:
            dist_class = "platykurtic"
        else:
            dist_class = "normal"

        correlations = {}
        regression_beta = None
        regression_r2 = None

        try:
            import json as _json

            spy_result = await mcp_client.call_tool_by_name(
                "get_prices", {"ticker": "SPY", "period": "1y", "interval": "1d"}
            )
            spy_raw = {}
            if hasattr(spy_result, "content"):
                for item in spy_result.content:
                    txt = item.text if hasattr(item, "text") else str(item)
                    try:
                        spy_raw = _json.loads(txt)
                    except (ValueError, TypeError):
                        continue
            spy_records = spy_raw.get("data", []) if isinstance(spy_raw, dict) else []
            spy_close_map = {}
            for row in spy_records:
                if not isinstance(row, dict):
                    continue
                d = row.get("Date") or row.get("date", "")
                c = row.get("Close") or row.get("close")
                if d and c is not None:
                    spy_close_map[d] = float(c)
            spy_closes = []
            common_dates = []
            for d in sorted_dates:
                if d in spy_close_map and d in price_data and price_data[d] is not None:
                    spy_closes.append(spy_close_map[d])
                    common_dates.append(d)

            if len(spy_closes) > 10 and len(common_dates) > 10:
                stock_returns = np.diff(
                    np.log([price_data[d] for d in common_dates if price_data[d] is not None])
                )
                spy_returns_arr = np.diff(np.log(spy_closes))
                min_len = min(len(stock_returns), len(spy_returns_arr))
                if min_len > 5:
                    sr = stock_returns[:min_len]
                    spr = spy_returns_arr[:min_len]
                    corr = np.corrcoef(sr, spr)[0, 1]
                    correlations["SPY"] = round(float(corr), 4)
                    beta = np.polyfit(spr, sr, 1)
                    regression_beta = round(float(beta[0]), 4)
                    ss_res = np.sum((sr - np.polyval(beta, spr)) ** 2)
                    ss_tot = np.sum((sr - np.mean(sr)) ** 2)
                    regression_r2 = round(float(1 - ss_res / ss_tot), 4) if ss_tot != 0 else 0
        except Exception as spy_err:
            logger.debug("SPY correlation failed (non-fatal): %s", spy_err)

        logger.debug(
            "Statistics: dist=%s skew=%.4f kurt=%.4f beta=%s",
            dist_class,
            skew,
            kurt,
            regression_beta,
        )
        return StatisticalSummary(
            return_distribution=dist_class,
            skewness=round(skew, 4),
            kurtosis=round(kurt, 4),
            jarque_bera_pvalue=round(float(jb_p), 4),
            correlations=correlations,
            regression_beta=regression_beta,
            regression_r_squared=regression_r2,
        ).model_dump()
    except Exception as e:
        logger.warning("Statistical analysis failed: %s", e)
        return StatisticalSummary().model_dump()
