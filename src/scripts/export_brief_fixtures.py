"""Export anonymized brief fixtures from a local SQLite database.

Usage:
    python scripts/export_brief_fixtures.py [--db DB_PATH] [--out DIR]

Strips user/session identifiers and writes anonymized JSON files
suitable for use as regression corpus fixtures.
"""

import argparse
import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def export_fixtures(db_path: str, out_dir: str) -> list[Path]:
    logger.info("Exporting brief fixtures from %s", db_path)
    try:
        db = sqlite3.connect(db_path)
    except sqlite3.Error as e:
        logger.error("Failed to connect to DB %s: %s", db_path, e)
        raise
    cursor = db.execute(
        "SELECT ticker, response_text, recommendation, confidence, analysis_date "
        "FROM ticker_briefs ORDER BY RANDOM() LIMIT 15"
    )
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for i, row in enumerate(cursor):
        ticker, response_text, recommendation, confidence, analysis_date = row
        fixture = {
            "response_text": response_text or "",
        }
        if recommendation:
            fixture["final_recommendation"] = recommendation
        fp = out / f"exported_{i:02d}_{ticker}.json"
        fp.write_text(json.dumps(fixture, indent=2))
        written.append(fp)
    db.close()
    logger.info("Exported %d fixtures to %s", len(written), out_dir)
    return written


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="./db/finsight_memory.db")
    parser.add_argument("--out", default="./tests/regression/corpus")
    args = parser.parse_args()
    files = export_fixtures(args.db, args.out)
    print(f"Exported {len(files)} fixtures to {args.out}")
