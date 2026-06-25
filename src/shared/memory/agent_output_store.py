"""Shared agent output store — persists full agent outputs to SQLite.

Allows the reviewer executor to fetch full agent outputs directly from the
database instead of receiving them through the LLM prompt, eliminating
token waste and avoiding the OpenAI Agents SDK tool-call limitation.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from shared.memory.store import get_db, write_lock

logger = logging.getLogger(__name__)

_AGENT_KEY_MAP: dict[str, str] = {
    "financial rag agent": "rag",
    "quant analysis agent": "quant",
    "market context agent": "market_context",
    "analytics agent": "analytics",
    "reviewer agent": "reviewer",
}


def _normalize_agent_key(resolved_name: str) -> str:
    lower = resolved_name.lower().strip()
    return _AGENT_KEY_MAP.get(lower, lower.replace(" ", "_"))


async def store_agent_output(session_id: str, agent_name: str, output: dict[str, Any]) -> None:
    """INSERT OR REPLACE agent output into the shared store."""
    async with write_lock():
        conn = await get_db()
        await conn.execute(
            "INSERT OR REPLACE INTO agent_output_store"
            " (session_id, agent_name, output_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                session_id,
                agent_name,
                json.dumps(output, default=str),
                datetime.now(UTC).isoformat(),
            ),
        )
        await conn.commit()
    logger.debug("Stored agent output: session=%s agent=%s", session_id, agent_name)


async def get_agent_outputs(session_id: str) -> dict[str, dict[str, Any]]:
    """Return all agent outputs for a session, keyed by normalized short name."""
    conn = await get_db()
    cursor = await conn.execute(
        "SELECT agent_name, output_json FROM agent_output_store WHERE session_id = ?",
        (session_id,),
    )
    rows = await cursor.fetchall()
    result: dict[str, dict[str, Any]] = {}
    for agent_name, output_json in rows:
        key = _normalize_agent_key(agent_name)
        try:
            result[key] = json.loads(output_json)
        except (json.JSONDecodeError, TypeError):
            result[key] = {"_raw_text": output_json[:5000]}
    logger.info(
        "Fetched %d agent outputs from shared store for session=%s", len(result), session_id
    )
    return result


async def prune_stale_outputs(max_age_seconds: int = 600) -> int:
    """Delete agent outputs older than max_age_seconds. Returns count deleted."""
    cutoff = datetime.now(UTC).timestamp() - max_age_seconds
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=UTC).isoformat()
    async with write_lock():
        conn = await get_db()
        cursor = await conn.execute(
            "DELETE FROM agent_output_store WHERE created_at < ?",
            (cutoff_iso,),
        )
        deleted = cursor.rowcount
        await conn.commit()
    if deleted:
        logger.info("Pruned %d stale agent outputs", deleted)
    return deleted
