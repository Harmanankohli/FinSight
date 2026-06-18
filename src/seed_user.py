"""Seed a test user into the local SQLite database for login testing.

Usage:
    uv run python src/seed_user.py
    uv run python src/seed_user.py --username admin --password admin123 --role admin
"""

import argparse
import asyncio
import logging
import sys

sys.path.insert(0, "src")

logger = logging.getLogger(__name__)


async def main(username: str, password: str, role: str) -> None:
    from shared.memory.user_store import create_user, ensure_schema_v4, get_user_by_username

    await ensure_schema_v4()

    username = username.strip().lower()
    existing = await get_user_by_username(username)
    if existing:
        logger.info("Seed user %s: already exists (role=%s)", username, existing["role"])
        print(f"User '{username}' already exists (id={existing['user_id']}, role={existing['role']})")
        return

    user_id = await create_user(username, password, role=role)
    logger.info("Seed user %s: created (role=%s)", username, role)
    print(f"Created user '{username}' (id={user_id}, role={role})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed a test user")
    parser.add_argument("--username", default="test", help="Username (default: test)")
    parser.add_argument("--password", default="test123", help="Password (default: test123)")
    parser.add_argument("--role", default="user", choices=["user", "admin"], help="Role (default: user)")
    args = parser.parse_args()
    asyncio.run(main(args.username, args.password, args.role))
