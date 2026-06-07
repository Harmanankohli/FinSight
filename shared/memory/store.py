"""SQLite foundation for the FinSight memory layer.

Provides async database connection management and schema migration.
All custom memory tables live alongside ADK's DatabaseSessionService tables
in the same SQLite database file.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

DB_PATH = Path(__file__).resolve().parent.parent.parent / "db" / "finsight_memory.db"

SCHEMA_VERSION = 3

CREATE_TABLES_SQL = """
-- Stores structured InvestmentBrief objects. Written by TickerMemory, read by agent prompt builders.
CREATE TABLE IF NOT EXISTS ticker_briefs (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    query TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    confidence REAL NOT NULL,
    brief_json TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL
);

-- Stores user risk profile and portfolio holdings. Written by PortfolioStore, read by analysis agents.
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id TEXT PRIMARY KEY,
    risk_profile TEXT NOT NULL DEFAULT 'medium',
    holdings TEXT NOT NULL DEFAULT '[]',
    horizon TEXT NOT NULL DEFAULT 'medium_term',
    updated_at TIMESTAMP NOT NULL
);

-- Tracks each BUY/HOLD/SELL recommendation with price snapshot. Written by PerformanceTracker, read by evaluate/get_accuracy_stats.
CREATE TABLE IF NOT EXISTS recommendation_records (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    user_id TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    confidence REAL NOT NULL,
    price_at_rec REAL,
    created_at TIMESTAMP NOT NULL,
    evaluated_at TIMESTAMP,
    realized_return REAL
);

-- Stores conversation events as searchable entries. Written by SQLiteMemoryService, read by search_memory.
CREATE TABLE IF NOT EXISTS memory_entries (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    content_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    search_text TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_briefs_ticker ON ticker_briefs(ticker);
CREATE INDEX IF NOT EXISTS idx_briefs_user ON ticker_briefs(user_id);
CREATE INDEX IF NOT EXISTS idx_briefs_created ON ticker_briefs(created_at);
CREATE INDEX IF NOT EXISTS idx_recs_ticker ON recommendation_records(ticker);
CREATE INDEX IF NOT EXISTS idx_recs_user ON recommendation_records(user_id);
CREATE INDEX IF NOT EXISTS idx_recs_evaluated ON recommendation_records(evaluated_at);
CREATE INDEX IF NOT EXISTS idx_memory_user ON memory_entries(user_id);
CREATE INDEX IF NOT EXISTS idx_memory_session ON memory_entries(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_created ON memory_entries(created_at);

-- Deduplicates SEC filing ingestion. Written by filing pipeline, read by is_filing_ingested.
CREATE TABLE IF NOT EXISTS ingested_filings (
    edgar_url TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    ingested_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingested_ticker ON ingested_filings(ticker);

-- Persists A2A task payloads across process restarts. Replaces InMemoryTaskStore.
CREATE TABLE IF NOT EXISTS a2a_tasks (
    task_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_a2a_tasks_owner ON a2a_tasks(owner);
"""


_db_conn: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()   # serialises writes
_init_lock = asyncio.Lock() # guards singleton creation


async def get_db(path: Path = DB_PATH) -> aiosqlite.Connection:
    """Return the long-lived singleton SQLite connection, creating it on first call."""
    global _db_conn
    if _db_conn is not None:
        return _db_conn
    async with _init_lock:
        if _db_conn is not None:
            return _db_conn
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        await init_db(conn)
        _db_conn = conn
    return _db_conn


def write_lock() -> asyncio.Lock:
    """Return the module-level write lock. Wrap all INSERT/UPDATE/DELETE calls with this."""
    return _db_lock


async def close_db() -> None:
    """Close the singleton connection. Call at process shutdown."""
    global _db_conn
    if _db_conn is not None:
        await _db_conn.close()
        _db_conn = None


# Version-based migration — schema_version tracks current version, CREATE TABLEs are idempotent, ALTER TABLEs use try/except for additive changes.
async def init_db(conn: aiosqlite.Connection) -> None:
    """Create all memory tables if they don't exist. Idempotent."""
    await conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    cursor = await conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    row = await cursor.fetchone()
    current_version = row[0] if row else 0

    if current_version < SCHEMA_VERSION:
        await conn.executescript(CREATE_TABLES_SQL)
        await conn.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        await conn.commit()

    # Migration v1→v2: adds search_text column for BM25 full-text search. Safe to re-run — noop if column exists.
    try:
        await conn.execute("ALTER TABLE memory_entries ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
        await conn.commit()
    except Exception:
        pass  # Column already exists

    # Migration v2→v3: adds analysis_date column to support date-ordered lookups independent of created_at.
    try:
        await conn.execute("ALTER TABLE ticker_briefs ADD COLUMN analysis_date TEXT")
        await conn.commit()
    except Exception:
        pass  # Column already exists

    # Migration v3→v4: creates ingested_filings table for SEC filing dedup. Added after initial schema release.
    try:
        await conn.execute("""CREATE TABLE IF NOT EXISTS ingested_filings (
            edgar_url TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            ingested_at TIMESTAMP NOT NULL
        )""")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ingested_ticker ON ingested_filings(ticker)")
        await conn.commit()
    except Exception:
        pass

    # Migration v4→v5: creates a2a_tasks table for persistent A2A task storage.
    try:
        await conn.execute("""CREATE TABLE IF NOT EXISTS a2a_tasks (
            task_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )""")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_a2a_tasks_owner ON a2a_tasks(owner)")
        await conn.commit()
    except Exception:
        pass


async def prune_old_records(days: int | None = None) -> dict[str, int]:
    """Delete records older than `days` from the three main memory tables.

    Reads MEMORY_RETENTION_DAYS env var if days is not supplied; defaults to 90.
    Returns a dict of {table: rows_deleted}. Does not VACUUM — run that manually
    to avoid blocking startup on a large DB rewrite.
    """
    if days is None:
        days = int(os.environ.get("MEMORY_RETENTION_DAYS", "90"))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = await get_db()
    _ALLOWED_TABLES: frozenset[str] = frozenset({
        "ticker_briefs", "recommendation_records", "memory_entries",
    })
    async with write_lock():
        deleted: dict[str, int] = {}
        for table in _ALLOWED_TABLES:
            cur = await conn.execute(
                f"DELETE FROM {table} WHERE created_at < ?", (cutoff,)
            )
            deleted[table] = cur.rowcount
        await conn.commit()
    return deleted


async def is_filing_ingested(edgar_url: str, db_path: Path = DB_PATH) -> bool:
    """Return True if this filing URL has already been ingested."""
    conn = await get_db(db_path)
    cursor = await conn.execute(
        "SELECT 1 FROM ingested_filings WHERE edgar_url = ? LIMIT 1", (edgar_url,)
    )
    return await cursor.fetchone() is not None


async def mark_filing_ingested(edgar_url: str, ticker: str, db_path: Path = DB_PATH) -> None:
    """Record that a filing has been ingested."""
    from datetime import datetime
    from shared.config import IST
    async with write_lock():
        conn = await get_db(db_path)
        await conn.execute(
            "INSERT OR IGNORE INTO ingested_filings (edgar_url, ticker, ingested_at) VALUES (?, ?, ?)",
            (edgar_url, ticker.upper(), datetime.now(IST).isoformat()),
        )
        await conn.commit()
