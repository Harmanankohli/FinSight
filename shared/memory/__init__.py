"""FinSight memory layer.

Provides persistent storage for:
- Session history (via ADK DatabaseSessionService)
- Conversation memory (via SQLiteMemoryService)
- Structured investment briefs (via TickerMemory)
- User portfolios (via PortfolioStore)
- Recommendation performance (via PerformanceTracker)
"""

# Session conversation history with hybrid BM25 + embedding search.
from shared.memory.memory_service import SQLiteMemoryService
# Recommendation outcomes with win-rate tracking.
from shared.memory.performance_tracker import PerformanceTracker
# User portfolio holdings and risk profile.
from shared.memory.portfolio_store import PortfolioStore
from shared.memory.store import DB_PATH, get_db, init_db
# Per-ticker investment briefs and recommendation history.
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
