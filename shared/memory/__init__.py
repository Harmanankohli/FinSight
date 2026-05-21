"""FinSight memory layer.

Provides persistent storage for:
- Session history (via ADK DatabaseSessionService)
- Conversation memory (via SQLiteMemoryService)
- Structured investment briefs (via TickerMemory)
- User portfolios (via PortfolioStore)
- Recommendation performance (via PerformanceTracker)
"""

from shared.memory.memory_service import SQLiteMemoryService
from shared.memory.performance_tracker import PerformanceTracker
from shared.memory.portfolio_store import PortfolioStore
from shared.memory.store import DB_PATH, get_db, init_db
from shared.memory.ticker_memory import TickerMemory

__all__ = [
    "DB_PATH",
    "get_db",
    "init_db",
    "SQLiteMemoryService",
    "TickerMemory",
    "PortfolioStore",
    "PerformanceTracker",
]
