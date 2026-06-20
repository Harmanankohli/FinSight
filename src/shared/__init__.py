"""FinSight shared infrastructure — core utilities, base abstractions, and factories."""

from shared.base_agent import BaseAgent  # noqa: F401
from shared.bootstrap import bootstrap  # noqa: F401
from shared.guardrails import is_off_topic  # noqa: F401
from shared.logging_config import (  # noqa: F401
    logged,
    logged_class,
    logged_sync,
    setup_file_logging,
)
from shared.settings import Settings, get_settings, reset_settings_for_tests  # noqa: F401
from shared.ticker_utils import (  # noqa: F401
    extract_holdings,
    extract_ticker,
    resolve_and_validate_ticker,
)

_LAZY_IMPORTS = {
    "build_agent_app": "shared.agent_server",
    "GenericAgentExecutor": "shared.generic_executor",
    "get_shared_mcp": "shared.mcp_client",
    "get_langfuse_client": "shared.observability",
    "init_instrumentation": "shared.observability",
    "compute_alpha": "shared.metrics",
    "compute_beta": "shared.metrics",
    "compute_cagr": "shared.metrics",
    "compute_calmar_ratio": "shared.metrics",
    "compute_information_ratio": "shared.metrics",
    "compute_rsi_wilder": "shared.metrics",
    "compute_sharpe_ratio": "shared.metrics",
    "compute_sortino_ratio": "shared.metrics",
    "metric_result": "shared.metrics",
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        import importlib

        mod = importlib.import_module(_LAZY_IMPORTS[name])
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BaseAgent",
    "Settings",
    "get_settings",
    "reset_settings_for_tests",
    "build_agent_app",
    "GenericAgentExecutor",
    "bootstrap",
    "logged",
    "logged_sync",
    "logged_class",
    "setup_file_logging",
    "get_shared_mcp",
    "get_langfuse_client",
    "init_instrumentation",
    "is_off_topic",
    "extract_ticker",
    "resolve_and_validate_ticker",
    "extract_holdings",
    "compute_alpha",
    "compute_beta",
    "compute_cagr",
    "compute_calmar_ratio",
    "compute_information_ratio",
    "compute_rsi_wilder",
    "compute_sharpe_ratio",
    "compute_sortino_ratio",
    "metric_result",
]
