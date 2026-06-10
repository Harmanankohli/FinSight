"""Per-ticker recommendation history storage.

Stores structured InvestmentBrief objects and provides compact context
summaries for prompt injection (~100-300 tokens max).
"""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from shared.config import IST
from shared.memory.store import DB_PATH, get_db, write_lock
from shared.models import InvestmentBrief


class TickerMemory:
    def __init__(self, db_path: Path = DB_PATH):
        self._db_path = db_path

    # Stores full structured InvestmentBrief (ticker, rec, confidence, rationale). Called after agent completes analysis.
    async def store_brief(
        self, brief: InvestmentBrief, user_id: str, session_id: str
    ) -> str:
        """Store a full InvestmentBrief. Returns the record ID."""
        record_id = str(uuid.uuid4())
        async with write_lock():
            conn = await get_db(self._db_path)
            await conn.execute(
                """INSERT INTO ticker_briefs
                   (id, ticker, session_id, user_id, query, recommendation,
                    confidence, brief_json, created_at, analysis_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_id,
                    brief.ticker,
                    session_id,
                    user_id,
                    brief.query_context.user_query,
                    brief.final_recommendation,
                    brief.confidence_score,
                    json.dumps(brief.model_dump(mode="json")),
                    brief.generated_at.isoformat(),
                    datetime.now(IST).date().isoformat(),
                ),
            )
            await conn.commit()
        logger.info("Stored brief for %s (id=%s, rec=%s, conf=%.2f)",
                    brief.ticker, record_id, brief.final_recommendation, brief.confidence_score)
        return record_id

    # Stores a lightweight text-only brief when no structured InvestmentBrief is available (e.g. fallback or non-agent responses).
    async def store_minimal(
        self,
        ticker: str,
        user_id: str,
        session_id: str,
        query: str,
        response_text: str,
        recommendation: str = "UNKNOWN",
        confidence: float = 0.5,
    ) -> str:
        """Store a minimal brief when no structured InvestmentBrief is available."""
        record_id = str(uuid.uuid4())
        async with write_lock():
            conn = await get_db(self._db_path)
            await conn.execute(
                """INSERT INTO ticker_briefs
                   (id, ticker, session_id, user_id, query, recommendation,
                    confidence, brief_json, created_at, analysis_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_id,
                    ticker,
                    session_id,
                    user_id,
                    query,
                    recommendation,
                    confidence,
                    json.dumps({"response_text": response_text[:5000]}),
                    datetime.now(IST).isoformat(),
                    datetime.now(IST).date().isoformat(),
                ),
            )
            await conn.commit()
        logger.info("Stored minimal brief for %s (id=%s)", ticker, record_id)
        return record_id

    # Replaces brief_json.response_text on an existing record. Used to overwrite the LLM's short
    # save_brief rationale with the full synthesized analysis after the agent turn completes.
    async def update_response_text(self, record_id: str, response_text: str) -> bool:
        conn = await get_db(self._db_path)
        cursor = await conn.execute(
            "SELECT brief_json FROM ticker_briefs WHERE id = ?", (record_id,)
        )
        row = await cursor.fetchone()
        if not row:
            logger.debug("Brief %s not found for update", record_id)
            return False
        try:
            data = json.loads(row[0]) if row[0] else {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        data["response_text"] = response_text[:10000]
        async with write_lock():
            conn = await get_db(self._db_path)
            await conn.execute(
                "UPDATE ticker_briefs SET brief_json = ? WHERE id = ?",
                (json.dumps(data), record_id),
            )
            await conn.commit()
        logger.debug("Updated response_text for %s", record_id)
        return True

    # Fetches most recent brief for a ticker, ordered by analysis_date then created_at descending.
    async def get_latest(
        self, ticker: str, user_id: Optional[str] = None
    ) -> Optional[dict]:
        """Get the most recent brief for a ticker."""
        conn = await get_db(self._db_path)
        if user_id:
            cursor = await conn.execute(
                """SELECT id, ticker, session_id, user_id, query, recommendation,
                          confidence, brief_json, created_at, analysis_date
                   FROM ticker_briefs
                   WHERE ticker = ? AND user_id = ?
                   ORDER BY COALESCE(analysis_date, created_at) DESC LIMIT 1""",
                (ticker.upper(), user_id),
            )
        else:
            cursor = await conn.execute(
                """SELECT id, ticker, session_id, user_id, query, recommendation,
                          confidence, brief_json, created_at, analysis_date
                   FROM ticker_briefs
                   WHERE ticker = ?
                   ORDER BY COALESCE(analysis_date, created_at) DESC LIMIT 1""",
                (ticker.upper(),),
            )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "ticker": row[1],
            "session_id": row[2],
            "user_id": row[3],
            "query": row[4],
            "recommendation": row[5],
            "confidence": row[6],
            "brief_json": row[7],
            "created_at": row[8],
            "analysis_date": row[9],
        }

    # Returns last N briefs for a ticker, newest first. Used for trend detection and context building.
    async def get_history(
        self, ticker: str, limit: int = 10, user_id: Optional[str] = None
    ) -> list[dict]:
        """Get last N briefs for a ticker, newest first."""
        conn = await get_db(self._db_path)
        if user_id:
            cursor = await conn.execute(
                """SELECT id, ticker, session_id, user_id, query, recommendation,
                          confidence, brief_json, created_at, analysis_date
                   FROM ticker_briefs
                   WHERE ticker = ? AND user_id = ?
                   ORDER BY COALESCE(analysis_date, created_at) DESC LIMIT ?""",
                (ticker.upper(), user_id, limit),
            )
        else:
            cursor = await conn.execute(
                """SELECT id, ticker, session_id, user_id, query, recommendation,
                          confidence, brief_json, created_at, analysis_date
                   FROM ticker_briefs
                   WHERE ticker = ?
                   ORDER BY COALESCE(analysis_date, created_at) DESC LIMIT ?""",
                (ticker.upper(), limit),
            )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "ticker": r[1],
                "session_id": r[2],
                "user_id": r[3],
                "query": r[4],
                "recommendation": r[5],
                "confidence": r[6],
                "brief_json": r[7],
                "created_at": r[8],
                "analysis_date": r[9],
            }
            for r in rows
        ]

    # Compares latest two briefs on a given field (default: recommendation). Detects upgrades/downgrades across analyses.
    async def has_changed(
        self, ticker: str, field: str = "recommendation"
    ) -> Optional[dict]:
        """Compare latest vs previous brief on a field. Returns {old, new, changed}."""
        history = await self.get_history(ticker, limit=2)
        if len(history) < 2:
            return None
        latest = history[0]
        previous = history[1]
        # G11 fix: old = previous (older), new = latest (newer)
        old_val = previous.get(field)
        new_val = latest.get(field)
        return {
            "old": old_val,
            "new": new_val,
            "changed": old_val != new_val,
        }

    # Builds a compact ~300-token summary for prompt injection. Keeps rec, confidence, date, change delta, and truncated rationale.
    async def format_context(self, ticker: str, max_tokens: int = 300) -> str:
        """Generate a compact memory summary for prompt injection.

        Budget: ~300 tokens (~1200 chars). Truncates rationale,
        keeps ticker + recommendation + confidence + date + change delta.
        """
        history = await self.get_history(ticker, limit=2)
        if not history:
            return ""

        latest = history[0]
        created = latest["created_at"]
        if "T" in created:
            created = created.split("T")[0]

        rec = latest["recommendation"]
        conf = latest["confidence"]

        lines = [
            f"Previous analysis for {ticker} ({created}): {rec}, confidence {conf:.2f}."
        ]

        if len(history) >= 2:
            prev = history[1]
            prev_date = prev["created_at"]
            if "T" in prev_date:
                prev_date = prev_date.split("T")[0]
            prev_rec = prev["recommendation"]
            if prev_rec != rec:
                _RANK = {"SELL": 0, "HOLD": 1, "BUY": 2}
                direction = "upgraded" if _RANK.get(rec, 1) > _RANK.get(prev_rec, 1) else "downgraded"  # noqa: E501
                lines.append(
                    f"Prior rec ({prev_date}): {prev_rec} "
                    f"-> {direction} to {rec}."
                )

        brief_data = latest.get("brief_json", "{}")
        try:
            data = json.loads(brief_data)
            rationale = data.get("recommendation_rationale", "") or data.get("response_text", "")
            if rationale:
                thesis = rationale[:200]
                lines.append(f"Thesis: {thesis}")
        except (json.JSONDecodeError, AttributeError):
            logger.debug("Could not parse brief_json for %s context summary", ticker)

        result = " ".join(lines)
        max_chars = max_tokens * 4
        if len(result) > max_chars:
            result = result[:max_chars] + "..."
        return result
