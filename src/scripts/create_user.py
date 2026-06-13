#!/usr/bin/env python3
"""CLI script to create a FinSight user. Never accepts a password as a CLI arg.

Usage:
    python scripts/create_user.py --username alice [--role admin]

Prompts for password interactively (not echoed).
Bootstrap path for the first admin user when AUTH_ENABLED=true.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys


async def main() -> None:
    parser = argparse.ArgumentParser(description="Create a FinSight user")
    parser.add_argument("--username", required=True, help="Username (lowercase, alphanumeric)")
    parser.add_argument("--role", default="user", choices=["user", "admin"], help="User role")
    args = parser.parse_args()

    username = args.username.strip().lower()
    if not username or not username.isalnum():
        print("Error: username must be non-empty and alphanumeric", file=sys.stderr)
        sys.exit(1)

    password = getpass.getpass("Password: ")
    if not password:
        print("Error: password cannot be empty", file=sys.stderr)
        sys.exit(1)

    # Bootstrap the app config first
    from shared.bootstrap import bootstrap

    bootstrap("create_user")

    from shared.memory.user_store import create_user

    try:
        user_id = await create_user(username, password, role=args.role)
        print(f"Created user: {username} (id={user_id[:8]}..., role={args.role})")
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
