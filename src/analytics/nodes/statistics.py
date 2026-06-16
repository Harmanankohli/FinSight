import logging

import numpy as np
from scipy import stats as scipy_stats

logger = logging.getLogger(__name__)


async def _compute_statistics(price_data: dict, mcp_client) -> dict:
    try:
        sorted_dates = sorted(price_data.keys())
        closes = [price_data[d] for d in sorted_dates if price_data[d] is not None]
        if len(closes) < 10:
            return {"return_distribution": None, "skewness": None, "kurtosis": None, "jarque_bera_pvalue": None, "correlations": {}, "regression_beta": None, "regression_r_squared": None}

        prices = np.array(closes, dtype=float)
        log_returns = np.diff(np.log(prices))
        if len(log_returns) < 5:
            return {"return_distribution": None, "skewness": None, "kurtosis": None, "jarque_bera_pvalue": None, "correlations": {}, "regression_beta": None, "regression_r_squared": None}

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
            spy_result = await mcp_client.call_tool_by_name("get_prices", {"ticker": "SPY", "period": "1y", "interval": "1d"})
            spy_content = spy_result.get("content", [])
            spy_data = {}
            for item in spy_content:
                if isinstance(item, dict) and "text" in item:
                    import json
                    spy_data = json.loads(item["text"]) if isinstance((json.loads(item["text"])), dict) else {}
            spy_closes = []
            common_dates = []
            for d in sorted_dates:
                if d in spy_data and isinstance(spy_data[d], dict):
                    sc = spy_data[d].get("Close") or spy_data[d].get("close")
                    if sc is not None and d in price_data and price_data[d] is not None:
                        spy_closes.append(float(sc))
                        common_dates.append(d)

            if len(spy_closes) > 10 and len(common_dates) > 10:
                stock_returns = np.diff(np.log([price_data[d] for d in common_dates if price_data[d] is not None]))
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

        return {
            "return_distribution": dist_class,
            "skewness": round(skew, 4),
            "kurtosis": round(kurt, 4),
            "jarque_bera_pvalue": round(float(jb_p), 4),
            "correlations": correlations,
            "regression_beta": regression_beta,
            "regression_r_squared": regression_r2,
        }
    except Exception as e:
        logger.warning("Statistical analysis failed: %s", e)
        return {"return_distribution": None, "skewness": None, "kurtosis": None, "jarque_bera_pvalue": None, "correlations": {}, "regression_beta": None, "regression_r_squared": None}
