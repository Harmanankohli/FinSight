"""Bootstrap — all import-time side effects, executed exactly once per process.

Call bootstrap(service_name) as the FIRST statement after stdlib imports in each
entrypoint, before any framework imports that might trigger model loads or asyncio use.
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shared.settings import Settings


def bootstrap(service_name: str) -> Settings:
    """Initialise the process: env vars, stdio, event loop, logging, langfuse.

    Must be called once at process startup, before any framework or model imports.
    Returns the Settings singleton so callers can read config after bootstrapping.
    """
    # Lazy import to avoid circular dependency -- settings reads env vars only
    from shared.settings import get_settings

    s = get_settings()

    logger = logging.getLogger(service_name)

    # G7: set OpenAI-compatible env vars before any framework import reads them
    os.environ.setdefault("OPENAI_API_BASE", s.llm_base_url)
    os.environ.setdefault("OPENAI_API_KEY", s.llm_api_key)

    # HuggingFace: skip network checks unless explicitly opted out
    os.environ.setdefault("HF_HUB_OFFLINE", "1" if s.hf_hub_offline else "0")

    # Fix stdio encoding and asyncio before any I/O or framework init
    _reconfigure_stdio_utf8()
    _set_win_event_loop_policy()

    # Fail fast if runtime env is misconfigured (e.g. missing API keys)
    s.validate_runtime()

    # Lazy import: logging_config depends on settings being ready
    from shared.logging_config import setup_file_logging

    setup_file_logging(service_name)

    # Per-service LOG_LEVEL overrides global default; both fall back to INFO
    _log_level = (
        os.environ.get(f"LOG_LEVEL_{service_name.upper().replace('-', '_')}")
        or os.environ.get("LOG_LEVEL", "INFO")
    )
    logger.info("Bootstrapping %s: LOG_LEVEL=%s, env=%s", service_name, _log_level, s.env)

    # Langfuse is a no-op when keys are not configured in settings
    if s.langfuse_public_key is not None and s.langfuse_secret_key is not None:
        from shared.observability import init_langfuse, shutdown_langfuse

        init_langfuse(service_name)
        # Ensure Langfuse flushes pending traces on process exit
        atexit.register(shutdown_langfuse)
        logger.info("Langfuse initialised for %s", service_name)
    else:
        logger.debug("Langfuse skipped — keys not configured")

    return s


def _reconfigure_stdio_utf8() -> None:
    """Reconfigure stdout/stderr to UTF-8 (guards against Windows cp1252 errors)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            # Best-effort: some environments (e.g. redirected pipes) may not support reconfigure
            pass


def _set_win_event_loop_policy() -> None:
    """Set WindowsSelectorEventLoopPolicy on Windows for asyncio compatibility.

    Windows defaults to ProactorEventLoop which does not support subprocesses.
    SelectorEventLoop is required for asyncio subprocess management.
    """
    import asyncio

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
