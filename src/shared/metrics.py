import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class MetricValue(float):
    """Float subclass that carries validation metadata.

    Behaves exactly like a float everywhere (math, Pydantic, isinstance checks)
    but also exposes .to_dict() for reporting and .status/.warning for validation.
    """

    def __new__(cls, value, methodology="", min_valid=-999, max_valid=999, warning=None):
        obj = super().__new__(cls, value if np.isfinite(value) else 0.0)
        obj._methodology = methodology
        obj._min_valid = min_valid
        obj._max_valid = max_valid
        obj._warning = warning
        if not np.isfinite(value):
            obj._status = "ERROR"
            obj._warning = obj._warning or f"Non-finite value ({value})"
        elif value < min_valid or value > max_valid:
            obj._status = "WARNING"
            obj._warning = obj._warning or f"Value {value} outside valid range [{min_valid}, {max_valid}]"
        else:
            obj._status = "VALID"
        return obj

    @property
    def methodology(self):
        return self._methodology

    @property
    def min_valid(self):
        return self._min_valid

    @property
    def max_valid(self):
        return self._max_valid

    @property
    def status(self):
        return self._status

    @property
    def warning(self):
        return self._warning

    def to_dict(self):
        return {
            "value": float(self),
            "methodology": self._methodology,
            "min_valid": self._min_valid,
            "max_valid": self._max_valid,
            "status": self._status,
            "warning": self._warning,
        }


def metric_result(
    value: float,
    methodology: str,
    min_valid: float = -999,
    max_valid: float = 999,
    warning: str | None = None,
) -> MetricValue:
    return MetricValue(value, methodology, min_valid, max_valid, warning)


def compute_rsi_wilder(prices: pd.Series, period: int = 14) -> float:
    delta = prices.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain.iloc[-1] / avg_loss.iloc[-1] if avg_loss.iloc[-1] > 0 else float("inf")
    rsi = 100.0 - (100.0 / (1.0 + rs)) if np.isfinite(rs) else 100.0
    if np.isnan(rsi):
        return 50.0
    return round(max(0.0, min(100.0, rsi)), 1)


def compute_sharpe_ratio(
    returns: np.ndarray | pd.Series,
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    returns_arr = np.asarray(returns, dtype=float)
    if len(returns_arr) < 2 or np.std(returns_arr) == 0:
        return 0.0
    excess = returns_arr - risk_free_rate / periods_per_year
    sharpe = np.mean(excess) / np.std(excess) * np.sqrt(periods_per_year)
    return float(sharpe) if np.isfinite(sharpe) else 0.0


def compute_beta(
    asset_returns: np.ndarray | pd.Series,
    benchmark_returns: np.ndarray | pd.Series,
) -> float:
    a = np.asarray(asset_returns, dtype=float)
    b = np.asarray(benchmark_returns, dtype=float)
    min_len = min(len(a), len(b))
    if min_len < 2:
        return 1.0
    a, b = a[-min_len:], b[-min_len:]
    cov = np.cov(a, b)
    if cov[1, 1] == 0:
        return 1.0
    return float(cov[0, 1] / cov[1, 1])


def compute_sortino_ratio(
    returns: np.ndarray | pd.Series,
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    returns_arr = np.asarray(returns, dtype=float)
    if len(returns_arr) < 2:
        return 0.0
    excess = returns_arr - risk_free_rate / periods_per_year
    downside = returns_arr[returns_arr < 0]
    downside_std = np.std(downside) if len(downside) > 1 else 0.0
    if downside_std == 0:
        return 0.0
    sortino = np.mean(excess) / downside_std * np.sqrt(periods_per_year)
    return float(sortino) if np.isfinite(sortino) else 0.0


def compute_calmar_ratio(cagr: float, max_drawdown: float) -> float:
    if max_drawdown >= 0:
        return 0.0
    calmar = cagr / abs(max_drawdown)
    return float(calmar) if np.isfinite(calmar) else 0.0


def compute_alpha(
    returns: np.ndarray | pd.Series,
    benchmark_returns: np.ndarray | pd.Series,
    risk_free_rate: float = 0.0,
    beta: float | None = None,
    periods_per_year: int = 252,
) -> float:
    r = np.asarray(returns, dtype=float)
    b = np.asarray(benchmark_returns, dtype=float)
    min_len = min(len(r), len(b))
    if min_len < 2:
        return 0.0
    r, b = r[-min_len:], b[-min_len:]
    rf_per = risk_free_rate / periods_per_year
    if beta is None:
        beta = compute_beta(r, b)
    expected = rf_per + beta * (np.mean(b) - rf_per)
    alpha = np.mean(r) - expected
    return float(alpha) * periods_per_year if np.isfinite(alpha) else 0.0


def compute_information_ratio(
    returns: np.ndarray | pd.Series,
    benchmark_returns: np.ndarray | pd.Series,
    periods_per_year: int = 252,
) -> float:
    r = np.asarray(returns, dtype=float)
    b = np.asarray(benchmark_returns, dtype=float)
    min_len = min(len(r), len(b))
    if min_len < 2:
        return 0.0
    r, b = r[-min_len:], b[-min_len:]
    active = r - b
    tracking_error = np.std(active)
    if tracking_error == 0:
        return 0.0
    ir = np.mean(active) / tracking_error * np.sqrt(periods_per_year)
    return float(ir) if np.isfinite(ir) else 0.0


def compute_cagr(values: list[float]) -> float | None:
    cleaned = [v for v in values if v is not None and np.isfinite(v) and v > 0]
    if len(cleaned) < 2:
        return None
    n = len(cleaned) - 1
    cagr = (cleaned[-1] / cleaned[0]) ** (1.0 / n) - 1.0
    return float(cagr) if np.isfinite(cagr) else None
