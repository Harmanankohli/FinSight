"""FinSight memory layer.

Provides persistent storage for:
- Session history (via ADK DatabaseSessionService)
- Conversation memory (via SQLiteMemoryService)
- Structured investment briefs (via TickerMemory)
- User portfolios (via PortfolioStore)
- Recommendation performance (via PerformanceTracker)
"""

from shared.memory.store import DB_PATH, get_db, init_db
from shared.memory.ticker_memory import TickerMemory


def __getattr__(name: str):
    if name == "SQLiteMemoryService":
        from shared.memory.memory_service import SQLiteMemoryService
        return SQLiteMemoryService
    if name == "PerformanceTracker":
        from shared.memory.performance_tracker import PerformanceTracker
        return PerformanceTracker
    if name == "PortfolioStore":
        from shared.memory.portfolio_store import PortfolioStore
        return PortfolioStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DB_PATH",
    "get_db",
    "init_db",
    "SQLiteMemoryService",
    "TickerMemory",
    "PortfolioStore",
    "PerformanceTracker",
]
