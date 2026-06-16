from dataclasses import dataclass
from typing import Any


@dataclass
class AnalyticsDeps:
    ticker: str
    period: str
    mcp_client: Any
    langfuse_handler: Any | None = None
