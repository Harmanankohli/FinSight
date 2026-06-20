"""Unit tests for the memory store database layer."""

from shared.memory.store import SCHEMA_VERSION, get_db, init_db


async def test_init_db_creates_tables(memory_db):
    conn = await get_db(memory_db)
    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {row[0] for row in await cursor.fetchall()}
    required = {
        "ticker_briefs",
        "user_profiles",
        "recommendation_records",
        "memory_entries",
        "ingested_filings",
        "a2a_tasks",
        "agent_output_store",
        "schema_version",
    }
    missing = required - tables
    assert not missing, f"Tables not created: {missing}"


async def test_init_db_idempotent(memory_db):
    """Calling init_db twice must not raise."""
    conn = await get_db(memory_db)
    await init_db(conn)  # second call on same DB
    cursor = await conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    row = await cursor.fetchone()
    assert row[0] == SCHEMA_VERSION


async def test_schema_version_written(memory_db):
    conn = await get_db(memory_db)
    cursor = await conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == SCHEMA_VERSION


async def test_wal_mode_enabled(memory_db):
    conn = await get_db(memory_db)
    cursor = await conn.execute("PRAGMA journal_mode")
    row = await cursor.fetchone()
    assert row[0].upper() == "WAL"


async def test_indexes_created(memory_db):
    conn = await get_db(memory_db)
    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
    indexes = {row[0] for row in await cursor.fetchall()}
    required_indexes = {
        "idx_briefs_ticker",
        "idx_briefs_user",
        "idx_recs_ticker",
        "idx_memory_user",
        "idx_memory_session",
        "idx_ingested_ticker",
        "idx_aos_session",
    }
    missing = required_indexes - indexes
    assert not missing, f"Indexes not created: {missing}"
