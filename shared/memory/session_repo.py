"""Session repository — replaces ad-hoc aiosqlite.connect() calls in api_routes.py.

Uses a lazy singleton for the ADK sessions DB so callers never open per-request
connections. The flatten helper is pure and independently testable.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from shared.memory.store import DB_PATH

logger = logging.getLogger(__name__)

_ADK_SESSION_DB = DB_PATH.parent / "adk_sessions.db"

_adk_conn: aiosqlite.Connection | None = None
_adk_init_lock = asyncio.Lock()


async def _get_adk_db() -> aiosqlite.Connection:
    """Lazy singleton for the ADK sessions SQLite DB."""
    global _adk_conn
    if _adk_conn is not None:
        return _adk_conn
    async with _adk_init_lock:
        if _adk_conn is not None:
            return _adk_conn
        _ADK_SESSION_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(_ADK_SESSION_DB))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        _adk_conn = conn
        logger.info("ADK sessions DB opened at %s", _ADK_SESSION_DB)
    return _adk_conn


def _flatten_event(data: dict) -> dict:
    """Extract author and content parts from a raw event JSON dict.

    Flattens functionCall / functionResponse parts into typed dicts.
    Pure function — no I/O, independently testable.
    """
    author = data.get("author") or data.get("role") or ""
    content_parts: list[dict] = []
    content = data.get("content", {})
    for part in (content.get("parts") or []):
        if not isinstance(part, dict):
            continue
        if part.get("text"):
            content_parts.append({"type": "text", "text": part["text"]})
        elif part.get("functionCall"):
            fc = part["functionCall"]
            content_parts.append({
                "type": "function_call",
                "name": fc.get("name", ""),
                "args": fc.get("args", {}),
            })
        elif part.get("functionResponse"):
            fr = part["functionResponse"]
            content_parts.append({
                "type": "function_response",
                "name": fr.get("name", ""),
                "response": str(fr.get("response", ""))[:500],
            })
    return {"author": author, "content": content_parts}


class SessionRepo:
    """Read-only access to ADK sessions and events."""

    async def list_sessions(
        self, user_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List sessions with event counts in a single JOIN query (no N+1)."""
        db = await _get_adk_db()
        if user_id:
            cursor = await db.execute(
                """
                SELECT s.id, s.user_id, s.app_name, s.update_time,
                       COUNT(e.id) AS event_count
                FROM sessions s
                LEFT JOIN events e ON e.session_id = s.id
                WHERE s.user_id = ?
                GROUP BY s.id
                ORDER BY s.update_time DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
        else:
            cursor = await db.execute(
                """
                SELECT s.id, s.user_id, s.app_name, s.update_time,
                       COUNT(e.id) AS event_count
                FROM sessions s
                LEFT JOIN events e ON e.session_id = s.id
                GROUP BY s.id
                ORDER BY s.update_time DESC
                LIMIT ?
                """,
                (limit,),
            )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "user_id": r[1],
                "app_name": r[2],
                "last_update_time": r[3],
                "event_count": r[4],
            }
            for r in rows
        ]

    async def get_events(self, session_id: str, user_id: str | None = None) -> list[dict[str, Any]] | None:
        """Return flattened events for a session. Returns None if session not found or not owned."""
        db = await _get_adk_db()
        if user_id:
            cursor = await db.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
            )
            if not await cursor.fetchone():
                return None
        cursor = await db.execute(
            "SELECT id, user_id, timestamp, event_data FROM events "
            "WHERE session_id = ? ORDER BY timestamp",
            (session_id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return None
        events = []
        for row in rows:
            try:
                data = json.loads(row[3]) if row[3] else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
            flat = _flatten_event(data)
            events.append({
                "id": row[0],
                "author": flat["author"],
                "timestamp": row[2],
                "content": flat["content"],
            })
        return events
