"""FinSight shared infrastructure — core utilities, base abstractions, and factories."""

from shared.agent_server import build_agent_app  # noqa: F401
from shared.base_agent import BaseAgent  # noqa: F401
from shared.bootstrap import bootstrap  # noqa: F401
from shared.generic_executor import GenericAgentExecutor  # noqa: F401
from shared.guardrails import is_off_topic  # noqa: F401
from shared.logging_config import (  # noqa: F401
    logged,
    logged_class,
    logged_sync,
    setup_file_logging,
)
from shared.mcp_client import get_shared_mcp  # noqa: F401
from shared.metrics import (  # noqa: F401
    compute_alpha,
    compute_beta,
    compute_cagr,
    compute_calmar_ratio,
    compute_information_ratio,
    compute_rsi_wilder,
    compute_sharpe_ratio,
    compute_sortino_ratio,
    metric_result,
)
from shared.observability import get_langfuse_client, init_instrumentation  # noqa: F401
from shared.settings import Settings, get_settings, reset_settings_for_tests  # noqa: F401
from shared.ticker_utils import (  # noqa: F401
    extract_holdings,
    extract_ticker,
    resolve_and_validate_ticker,
)

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
