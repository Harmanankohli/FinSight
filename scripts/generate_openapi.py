#!/usr/bin/env python3
"""Generate docs/openapi.json from the FastAPI spec app.

Usage:
    python scripts/generate_openapi.py
    python scripts/generate_openapi.py --check   # exit 1 if file differs
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_1_adk.api_fastapi import spec

OPENAPI_PATH = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


def main() -> None:
    serialized = json.dumps(spec, indent=2, default=str) + "\n"

    if "--check" in sys.argv:
        current = OPENAPI_PATH.read_text(encoding="utf-8") if OPENAPI_PATH.exists() else ""
        if current != serialized:
            print(f"OpenAPI spec changed — regenerate with: python scripts/generate_openapi.py")
            sys.exit(1)
        print("OpenAPI spec is up to date.")
        return

    OPENAPI_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPENAPI_PATH.write_text(serialized, encoding="utf-8")
    print(f"Wrote {OPENAPI_PATH}")


if __name__ == "__main__":
    main()
