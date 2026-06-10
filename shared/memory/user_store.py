"""User authentication storage — create/verify users, manage refresh tokens.

Schema lives in ticker_briefs DB (`get_db()`) alongside the existing tables.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from shared.memory.store import get_db, write_lock

logger = logging.getLogger(__name__)

try:
    from argon2 import PasswordHasher
    _ph = PasswordHasher()
except ImportError:
    _ph = None  # type: ignore[assignment]

    class _FallbackHasher:
        def hash(self, pwd: str) -> str:
            return f"plain:{pwd}"

        def verify(self, hash_str: str, pwd: str) -> bool:
            return hash_str == f"plain:{pwd}"

        def check_needs_rehash(self, hash_str: str) -> bool:
            return False

    _ph = _FallbackHasher()  # type: ignore[assignment]


_SCHEMA_V4_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    created_at TIMESTAMP NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    jti TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    rotated_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
"""


async def ensure_schema_v4() -> None:
    """Run schema v4 migration (users + refresh_tokens tables). Idempotent."""
    conn = await get_db()
    await conn.executescript(_SCHEMA_V4_SQL)
    await conn.commit()


async def create_user(
    username: str, password: str, role: str = "user"
) -> str:
    """Create a new user. Returns the user_id. Raises on duplicate username."""
    await ensure_schema_v4()
    user_id = str(uuid.uuid4())
    pw_hash = _ph.hash(password)
    now = datetime.now(timezone.utc).isoformat()
    async with write_lock():
        conn = await get_db()
        try:
            await conn.execute(
                "INSERT INTO users (user_id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, pw_hash, role, now),
            )
            await conn.commit()
        except Exception as exc:
            raise ValueError(f"Username '{username}' may already exist") from exc
    logger.info("Created user %s (role=%s)", username, role)
    return user_id


async def get_user(user_id: str) -> dict | None:
    """Look up a user by ID. Returns None if not found."""
    await ensure_schema_v4()
    conn = await get_db()
    cursor = await conn.execute(
        "SELECT user_id, username, password_hash, role, created_at, disabled FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "user_id": row[0],
        "username": row[1],
        "password_hash": row[2],
        "role": row[3],
        "created_at": row[4],
        "disabled": bool(row[5]),
    }


async def get_user_by_username(username: str) -> dict | None:
    """Look up a user by username. Returns None if not found."""
    await ensure_schema_v4()
    conn = await get_db()
    cursor = await conn.execute(
        "SELECT user_id, username, password_hash, role, created_at, disabled FROM users WHERE username = ?",
        (username,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "user_id": row[0],
        "username": row[1],
        "password_hash": row[2],
        "role": row[3],
        "created_at": row[4],
        "disabled": bool(row[5]),
    }


async def verify_password(username: str, password: str) -> dict | None:
    """Verify a username/password combo. Returns user dict on success, None on failure."""
    user = await get_user_by_username(username)
    if not user:
        return None
    if user["disabled"]:
        return None
    try:
        _ph.verify(user["password_hash"], password)
    except Exception:
        return None
    # Rehash on param change (argon2 recommended practice)
    if _ph.check_needs_rehash(user["password_hash"]):
        new_hash = _ph.hash(password)
        async with write_lock():
            conn = await get_db()
            await conn.execute(
                "UPDATE users SET password_hash = ? WHERE user_id = ?",
                (new_hash, user["user_id"]),
            )
            await conn.commit()
    return user


# ── Refresh token management ────────────────────────────────────────────────


async def store_refresh_token(jti: str, user_id: str, expires_at: str) -> None:
    """Persist a refresh token for rotation/reuse detection."""
    await ensure_schema_v4()
    async with write_lock():
        conn = await get_db()
        await conn.execute(
            "INSERT INTO refresh_tokens (jti, user_id, expires_at) VALUES (?, ?, ?)",
            (jti, user_id, expires_at),
        )
        await conn.commit()
    logger.debug("Stored refresh token jti=%s for user=%s", jti[:8], user_id)


async def revoke_refresh_token(jti: str) -> None:
    """Revoke a specific refresh token."""
    await ensure_schema_v4()
    async with write_lock():
        conn = await get_db()
        await conn.execute(
            "UPDATE refresh_tokens SET revoked = 1 WHERE jti = ?", (jti,)
        )
        await conn.commit()


async def is_refresh_token_revoked(jti: str) -> bool:
    """Check if a refresh token has been revoked."""
    await ensure_schema_v4()
    conn = await get_db()
    cursor = await conn.execute(
        "SELECT revoked FROM refresh_tokens WHERE jti = ?", (jti,)
    )
    row = await cursor.fetchone()
    if not row:
        return True  # unknown = revoked for safety
    return bool(row[0])


async def get_refresh_token(jti: str) -> dict | None:
    """Get full refresh token record."""
    await ensure_schema_v4()
    conn = await get_db()
    cursor = await conn.execute(
        "SELECT jti, user_id, expires_at, revoked, rotated_at FROM refresh_tokens WHERE jti = ?",
        (jti,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "jti": row[0],
        "user_id": row[1],
        "expires_at": row[2],
        "revoked": bool(row[3]),
        "rotated_at": row[4],
    }


async def revoke_user_refresh_tokens(user_id: str) -> int:
    """Revoke all refresh tokens for a user. Returns count."""
    await ensure_schema_v4()
    async with write_lock():
        conn = await get_db()
        cursor = await conn.execute(
            "UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ? AND revoked = 0",
            (user_id,),
        )
        await conn.commit()
        return cursor.rowcount


async def update_rotated_at(jti: str) -> None:
    """Mark when a refresh token was rotated (for grace-window logic)."""
    await ensure_schema_v4()
    now = datetime.now(timezone.utc).isoformat()
    async with write_lock():
        conn = await get_db()
        await conn.execute(
            "UPDATE refresh_tokens SET rotated_at = ? WHERE jti = ?", (now, jti)
        )
        await conn.commit()
