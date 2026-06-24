"""FinSight memory layer.

Provides persistent storage for:
- Session history (via ADK DatabaseSessionService)
- Conversation memory (via SQLiteMemoryService)
- Structured investment briefs (via TickerMemory)
- User portfolios (via PortfolioStore)
- Recommendation performance (via PerformanceTracker)
"""

from shared.memory.agent_output_store import (
    get_agent_outputs,
    prune_stale_outputs,
    store_agent_output,
)
from shared.memory.store import DB_PATH, get_db, init_db
from shared.memory.ticker_memory import TickerMemory

_LAZY_IMPORTS = {
    "SQLiteMemoryService": "shared.memory.memory_service",
    "PerformanceTracker": "shared.memory.performance_tracker",
    "PortfolioStore": "shared.memory.portfolio_store",
}


def __getattr__(name: str) -> object:
    if name in _LAZY_IMPORTS:
        import importlib

        mod = importlib.import_module(_LAZY_IMPORTS[name])
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DB_PATH",
    "get_db",
    "init_db",
    "SQLiteMemoryService",
    "TickerMemory",
    "PortfolioStore",
    "PerformanceTracker",
    "store_agent_output",
    "get_agent_outputs",
    "prune_stale_outputs",
]
