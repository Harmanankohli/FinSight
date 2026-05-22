"""SQLite foundation for the FinSight memory layer.

Provides async database connection management and schema migration.
All custom memory tables live alongside ADK's DatabaseSessionService tables
in the same SQLite database file.
"""

from pathlib import Path

import aiosqlite

DB_PATH = Path(__file__).resolve().parent.parent.parent / "finsight_memory.db"

SCHEMA_VERSION = 2

CREATE_TABLES_SQL = """
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

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id TEXT PRIMARY KEY,
    risk_profile TEXT NOT NULL DEFAULT 'medium',
    holdings TEXT NOT NULL DEFAULT '[]',
    horizon TEXT NOT NULL DEFAULT 'medium_term',
    updated_at TIMESTAMP NOT NULL
);

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

CREATE TABLE IF NOT EXISTS ingested_filings (
    edgar_url TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    ingested_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingested_ticker ON ingested_filings(ticker);
"""


async def get_db(path: Path = DB_PATH) -> aiosqlite.Connection:
    """Open an async SQLite connection with WAL mode and foreign keys.

    Automatically runs schema migration on first use.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path))
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    await init_db(conn)
    return conn


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

    # Migration: add search_text column if missing
    try:
        await conn.execute("ALTER TABLE memory_entries ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
        await conn.commit()
    except Exception:
        pass  # Column already exists

    # Migration: add ingested_filings table if missing
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


async def is_filing_ingested(edgar_url: str, db_path: Path = DB_PATH) -> bool:
    """Return True if this filing URL has already been ingested."""
    conn = await get_db(db_path)
    try:
        cursor = await conn.execute(
            "SELECT 1 FROM ingested_filings WHERE edgar_url = ? LIMIT 1", (edgar_url,)
        )
        return await cursor.fetchone() is not None
    finally:
        await conn.close()


async def mark_filing_ingested(edgar_url: str, ticker: str, db_path: Path = DB_PATH) -> None:
    """Record that a filing has been ingested."""
    from datetime import datetime
    conn = await get_db(db_path)
    try:
        await conn.execute(
            "INSERT OR IGNORE INTO ingested_filings (edgar_url, ticker, ingested_at) VALUES (?, ?, ?)",
            (edgar_url, ticker.upper(), datetime.utcnow().isoformat()),
        )
        await conn.commit()
    finally:
        await conn.close()
