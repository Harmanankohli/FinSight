# ruff: noqa: E402
"""Recommendation outcome tracking.

Records each BUY/HOLD/SELL recommendation with optional price snapshot.
Can evaluate past recommendations against current market prices via yfinance.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from shared.memory.store import DB_PATH, get_db, write_lock
from shared.settings import IST


class PerformanceTracker:
    def __init__(self, db_path: Path = DB_PATH):
        self._db_path = db_path

    # Persists recommendation with async price snapshot. Falls back to yfinance fetch via executor if no price supplied.  # noqa: E501
    async def record_recommendation(
        self,
        ticker: str,
        user_id: str,
        recommendation: str,
        confidence: float,
        price: float | None = None,
    ) -> str:
        """Record a new recommendation with a live price snapshot.

        If price is not supplied, fetches from yfinance asynchronously so
        realized_return can be computed at evaluation time.
        """
        if price is None:
            import asyncio as _asyncio

            loop = _asyncio.get_event_loop()
            price = await loop.run_in_executor(None, self._fetch_current_price, ticker)

        record_id = str(uuid.uuid4())
        async with write_lock():
            conn = await get_db(self._db_path)
            await conn.execute(
                """INSERT INTO recommendation_records
                   (id, ticker, user_id, recommendation, confidence,
                    price_at_rec, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_id,
                    ticker.upper(),
                    user_id,
                    recommendation.upper(),
                    confidence,
                    price,
                    datetime.now(IST).isoformat(),
                ),
            )
            await conn.commit()
        logger.info(
            "Recorded %s recommendation for %s (conf=%.2f, price=%s)",
            recommendation.upper(),
            ticker,
            confidence,
            price,
        )
        return record_id

    # Batch-evaluates all unevaluated recommendations by fetching current prices and computing realized_return for each.  # noqa: E501
    async def evaluate_all(self) -> list[dict[str, Any]]:
        """Compare all unevaluated recommendations to current prices.

        Fetches current price via yfinance, calculates realized_return.
        """
        conn = await get_db(self._db_path)
        cursor = await conn.execute(
            """SELECT id, ticker, recommendation, price_at_rec
               FROM recommendation_records
               WHERE evaluated_at IS NULL AND price_at_rec IS NOT NULL"""
        )
        rows = list(await cursor.fetchall())
        logger.info("Evaluating %d unevaluated recommendations", len(rows))

        results = []
        for row in rows:
            record_id, ticker, recommendation, price_at_rec = row
            loop = asyncio.get_event_loop()
            current_price = await loop.run_in_executor(None, self._fetch_current_price, ticker)
            if current_price is None:
                logger.debug("Skipping %s evaluation: no current price", ticker)
                continue

            if price_at_rec == 0:
                logger.debug("Skipping %s evaluation: zero price_at_rec", ticker)
                continue
            realized_return = (current_price - price_at_rec) / price_at_rec
            now = datetime.now(IST).isoformat()

            async with write_lock():
                conn = await get_db(self._db_path)
                await conn.execute(
                    """UPDATE recommendation_records
                       SET evaluated_at = ?, realized_return = ?
                       WHERE id = ?""",
                    (now, realized_return, record_id),
                )
                await conn.commit()

            results.append(
                {
                    "id": record_id,
                    "ticker": ticker,
                    "recommendation": recommendation,
                    "price_at_rec": price_at_rec,
                    "current_price": current_price,
                    "realized_return": round(realized_return, 4),
                }
            )

        logger.info("Evaluation complete: %d updated", len(results))
        return results

    # Computes win rate per recommendation type. BUY correct if price rose (ret > 0), SELL correct if price fell (ret < 0). HOLD always counted as correct.  # noqa: E501
    async def get_accuracy_stats(self, user_id: str | None = None) -> dict[str, dict[str, Any]]:
        """Return win rate by recommendation type.

        A BUY is 'correct' if realized_return > 0.
        A SELL is 'correct' if realized_return < 0.
        HOLD is always counted as correct — penalising a neutral signal on price
        movement would bias the metric against conservative recommendations.
        """
        conn = await get_db(self._db_path)
        if user_id:
            cursor = await conn.execute(
                """SELECT recommendation, realized_return
                   FROM recommendation_records
                   WHERE user_id = ? AND evaluated_at IS NOT NULL
                   AND realized_return IS NOT NULL""",
                (user_id,),
            )
        else:
            cursor = await conn.execute(
                """SELECT recommendation, realized_return
                   FROM recommendation_records
                   WHERE evaluated_at IS NOT NULL
                   AND realized_return IS NOT NULL"""
            )
        rows = await cursor.fetchall()

        stats: dict[str, dict[str, Any]] = {}
        for rec, ret in rows:
            rec = rec.upper()
            if rec not in stats:
                stats[rec] = {"total": 0, "correct": 0, "avg_return": 0.0, "returns": []}
            stats[rec]["total"] += 1
            stats[rec]["returns"].append(ret)

            if rec == "BUY" and ret > 0:
                stats[rec]["correct"] += 1
            elif rec == "SELL" and ret < 0:
                stats[rec]["correct"] += 1
            elif rec == "HOLD":
                stats[rec]["correct"] += 1

        for rec in stats:
            returns = stats[rec]["returns"]
            stats[rec]["avg_return"] = round(sum(returns) / len(returns), 4) if returns else 0.0
            stats[rec]["win_rate"] = (
                round(stats[rec]["correct"] / stats[rec]["total"], 4)
                if stats[rec]["total"] > 0
                else 0.0
            )
            del stats[rec]["returns"]

        return stats

    async def get_past_recommendations(
        self, ticker: str, days: int = 30, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get recommendations for a ticker in the last N days."""
        conn = await get_db(self._db_path)
        cutoff = (datetime.now(IST) - timedelta(days=days)).isoformat()
        if user_id:
            cursor = await conn.execute(
                """SELECT id, ticker, user_id, recommendation, confidence,
                          price_at_rec, created_at, evaluated_at, realized_return
                   FROM recommendation_records
                   WHERE ticker = ? AND user_id = ? AND created_at >= ?
                   ORDER BY created_at DESC""",
                (ticker.upper(), user_id, cutoff),
            )
        else:
            cursor = await conn.execute(
                """SELECT id, ticker, user_id, recommendation, confidence,
                          price_at_rec, created_at, evaluated_at, realized_return
                   FROM recommendation_records
                   WHERE ticker = ? AND created_at >= ?
                   ORDER BY created_at DESC""",
                (ticker.upper(), cutoff),
            )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "ticker": r[1],
                "user_id": r[2],
                "recommendation": r[3],
                "confidence": r[4],
                "price_at_rec": r[5],
                "created_at": r[6],
                "evaluated_at": r[7],
                "realized_return": r[8],
            }
            for r in rows
        ]

    @staticmethod
    def _fetch_current_price(ticker: str) -> float | None:
        """Fetch current price via yfinance. Returns None on failure."""
        try:
            import yfinance as yf

            ticker_obj = yf.Ticker(ticker)
            info = ticker_obj.info
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            return float(price) if price is not None else None
        except Exception:
            logger.warning("Price fetch failed for %s, returning None", ticker)
            return None
