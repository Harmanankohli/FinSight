"""Immutable dependencies injected into every PydanticAI graph node.

Carries the ticker/period context, a shared MCP client for data fetching,
and an optional Langfuse handler for trace instrumentation.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class AnalyticsDeps:
    """Dependencies injected into graph nodes — immutable across a single run."""

    ticker: str                          # Stock ticker being analysed
    period: str                          # Lookback period string
    mcp_client: Any                      # Shared MCP client for data tools
    langfuse_handler: Any | None = None  # Optional Langfuse observability handler
