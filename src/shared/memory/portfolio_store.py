# ruff: noqa: E402
"""User portfolio and risk profile persistence.

Auto-captures portfolio holdings from QueryContext on each query.
Merges holdings over time — users never need to explicitly "set" their portfolio.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from shared.memory.store import DB_PATH, get_db, write_lock
from shared.models import QueryContext
from shared.settings import IST


class PortfolioStore:
    def __init__(self, db_path: Path = DB_PATH):
        self._db_path = db_path

    async def get_profile(self, user_id: str) -> Optional[dict]:
        """Get a user's profile including holdings, risk profile, and horizon."""
        conn = await get_db(self._db_path)
        cursor = await conn.execute(
            """SELECT user_id, risk_profile, holdings, horizon, updated_at
               FROM user_profiles WHERE user_id = ?""",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            logger.debug("Profile for user %s: not found", user_id)
            return None
        result = {
            "user_id": row[0],
            "risk_profile": row[1],
            "holdings": json.loads(row[2]),
            "horizon": row[3],
            "updated_at": row[4],
        }
        logger.debug("Profile for user %s: found (%d holdings)", user_id, len(result["holdings"]))
        return result

    # Called on each query. Merges new holdings with existing ones (union), updates risk/horizon only when non-empty.  # noqa: E501
    async def upsert_from_context(self, ctx: QueryContext) -> None:
        """Auto-capture portfolio from QueryContext on each query.

        Merges new holdings into existing profile. Updates risk_profile
        and horizon if they are non-empty and different from current.
        Uses user_id when available (auth mode), falls back to session_id.
        """
        effective_id = ctx.user_id or ctx.session_id
        conn = await get_db(self._db_path)
        existing = await conn.execute(
            """SELECT holdings, risk_profile, horizon FROM user_profiles
               WHERE user_id = ?""",
            (effective_id,),
        )
        row = await existing.fetchone()

        now = datetime.now(IST).isoformat()
        new_holdings = list(set(ctx.portfolio_holdings))

        async with write_lock():
            conn = await get_db(self._db_path)
            if row:
                existing_holdings = json.loads(row[0])
                merged = list(set(existing_holdings + new_holdings))
                risk = ctx.user_risk_profile if ctx.user_risk_profile else row[1]
                horizon = ctx.investment_horizon if ctx.investment_horizon else row[2]
                await conn.execute(
                    """UPDATE user_profiles
                       SET holdings = ?, risk_profile = ?, horizon = ?, updated_at = ?
                       WHERE user_id = ?""",
                    (json.dumps(merged), risk, horizon, now, effective_id),
                )
            else:
                risk = ctx.user_risk_profile or "medium"
                horizon = ctx.investment_horizon or "medium_term"
                await conn.execute(
                    """INSERT INTO user_profiles
                       (user_id, risk_profile, holdings, horizon, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (effective_id, risk, json.dumps(new_holdings), horizon, now),
                )
            await conn.commit()
        logger.debug(
            "Portfolio upserted for user %s (%d holdings)", effective_id, len(new_holdings)
        )

    # Simple read — returns the user's current holdings list from their profile.
    async def get_holdings(self, user_id: str) -> list[str]:
        """Get a user's current portfolio holdings."""
        profile = await self.get_profile(user_id)
        if profile:
            return profile["holdings"]
        return []

    # Simple write — replaces holdings outright via upsert (INSERT ... ON CONFLICT DO UPDATE).
    async def update_holdings(self, user_id: str, holdings: list[str]) -> None:
        """Explicitly set a user's portfolio holdings."""
        now = datetime.now(IST).isoformat()
        async with write_lock():
            conn = await get_db(self._db_path)
            await conn.execute(
                """INSERT INTO user_profiles (user_id, holdings, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE
                   SET holdings = ?, updated_at = ?""",
                (
                    user_id,
                    json.dumps(holdings),
                    now,
                    json.dumps(holdings),
                    now,
                ),
            )
            await conn.commit()
