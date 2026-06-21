"""Anomaly detection for the analytics agent.

Detects price spikes (>2.5σ z-score on log-returns), volume anomalies
(>3σ z-score), and fundamental outliers (PE <5 or >100, D/E >5).
Severity is rated none/low/medium/high based on anomaly count and
maximum z-score magnitude.
"""

import logging

import numpy as np

from shared.agent_models import AnomalyReport

logger = logging.getLogger(__name__)


async def _detect_anomalies(
    price_data: dict, ohlcv_data: list[dict], fundamentals_data: dict
) -> dict:
    """Detect price, volume, and fundamental anomalies via z-score.

    Price spikes: z-score > 2.5 on log returns.
    Volume spikes: z-score > 3.0 on raw volume.
    Fundamentals: PE < 5 or > 100, D/E > 5.
    Severity thresholds: none (0), low (≤2 anomalies, z<3.5),
    medium (≤5, z<4.5), high (otherwise).
    """
    try:
        price_anomalies = []
        volume_anomalies = []
        fundamental_anomalies = []

        sorted_dates = sorted(price_data.keys())
        closes = [price_data[d] for d in sorted_dates if price_data[d] is not None]
        if len(closes) >= 10:
            prices = np.array(closes, dtype=float)
            log_returns = np.diff(np.log(prices))
            if len(log_returns) > 5:
                mean_ret = np.mean(log_returns)
                std_ret = np.std(log_returns)
                for i, ret in enumerate(log_returns):
                    z_score = (ret - mean_ret) / std_ret if std_ret > 0 else 0
                    if abs(z_score) > 2.5:
                        date_idx = i + 1
                        date = (
                            sorted_dates[date_idx]
                            if date_idx < len(sorted_dates)
                            else f"idx-{date_idx}"
                        )
                        price_anomalies.append(
                            {
                                "date": date,
                                "return_pct": round(float(ret) * 100, 2),
                                "z_score": round(float(z_score), 2),
                                "type": "price_spike" if z_score > 0 else "price_drop",
                            }
                        )

        if ohlcv_data and len(ohlcv_data) >= 10:
            volumes = [d.get("volume", 0) for d in ohlcv_data if d.get("volume", 0) > 0]
            if volumes:
                vol_arr = np.array(volumes, dtype=float)
                vol_mean = np.mean(vol_arr)
                vol_std = np.std(vol_arr)
                for d in ohlcv_data:
                    v = float(d.get("volume", 0))
                    if v > 0 and vol_std > 0:
                        z_score = (v - vol_mean) / vol_std
                        if abs(z_score) > 3.0:
                            volume_anomalies.append(
                                {
                                    "date": d.get("date", ""),
                                    "volume": int(v),
                                    "z_score": round(float(z_score), 2),
                                    "type": "high_volume" if z_score > 0 else "low_volume",
                                }
                            )

        if fundamentals_data:
            pe = fundamentals_data.get("trailingPE") or fundamentals_data.get("trailing_pe")
            de = fundamentals_data.get("debtToEquity") or fundamentals_data.get("debt_to_equity")
            if pe is not None and (pe < 5 or pe > 100):
                fundamental_anomalies.append(f"P/E ratio {pe:.1f} outside normal range (5-100)")
            if de is not None and de > 5:
                fundamental_anomalies.append(f"Debt/Equity {de:.2f} exceeds 5.0 threshold")

        anomaly_count = len(price_anomalies) + len(volume_anomalies) + len(fundamental_anomalies)
        all_anomalies = price_anomalies + volume_anomalies
        max_z = max((a.get("z_score", 0) for a in all_anomalies), default=0)

        if anomaly_count == 0:
            severity = "none"
        elif anomaly_count <= 2 and max_z < 3.5:
            severity = "low"
        elif anomaly_count <= 5 and max_z < 4.5:
            severity = "medium"
        else:
            severity = "high"

        logger.info("Anomaly detection: count=%d severity=%s", anomaly_count, severity)
        return AnomalyReport(
            price_anomalies=price_anomalies,
            volume_anomalies=volume_anomalies,
            fundamental_anomalies=fundamental_anomalies,
            anomaly_count=anomaly_count,
            severity=severity,
        ).model_dump()
    except Exception as e:
        logger.warning("Anomaly detection failed: %s", e)
        return AnomalyReport().model_dump()
