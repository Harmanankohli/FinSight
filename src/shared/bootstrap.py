"""Bootstrap — all import-time side effects, executed exactly once per process.

Call bootstrap(service_name) as the FIRST statement after stdlib imports in each
entrypoint, before any framework imports that might trigger model loads or asyncio use.
"""

from __future__ import annotations

import atexit
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shared.settings import Settings


def bootstrap(service_name: str) -> "Settings":
    """Initialise the process: env vars, stdio, event loop, logging, langfuse.

    Returns the Settings singleton so callers can read config after bootstrapping.
    """
    from shared.settings import get_settings

    s = get_settings()

    # G7: set OpenAI-compatible env vars before any framework import reads them
    os.environ.setdefault("OPENAI_API_BASE", s.llm_base_url)
    os.environ.setdefault("OPENAI_API_KEY", s.llm_api_key)

    # HuggingFace: skip network checks unless explicitly opted out
    os.environ.setdefault("HF_HUB_OFFLINE", "1" if s.hf_hub_offline else "0")

    _reconfigure_stdio_utf8()
    _set_win_event_loop_policy()

    s.validate_runtime()

    from shared.logging_config import setup_file_logging

    setup_file_logging(service_name)

    # Langfuse is a no-op when keys are not configured
    if s.langfuse_public_key is not None and s.langfuse_secret_key is not None:
        from shared.observability import init_langfuse, shutdown_langfuse

        init_langfuse(service_name)
        atexit.register(shutdown_langfuse)

    return s


def _reconfigure_stdio_utf8() -> None:
    """Reconfigure stdout/stderr to UTF-8 (guards against Windows cp1252 errors)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def _set_win_event_loop_policy() -> None:
    """Set WindowsSelectorEventLoopPolicy on Windows for asyncio compatibility."""
    import asyncio

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
