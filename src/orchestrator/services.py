# ruff: noqa: E402
"""Register custom memory service for FinSight.

This file is automatically loaded by ADK when `adk web` starts.
It registers our SQLiteMemoryService under the 'finsight' URI scheme.

ADK's single-agent detection rewrites agents_dir to the parent directory
(src/orchestrator/) when src/orchestrator/web/agent.py exists, so this
file must live here — not inside web/ — for load_services_module() to
find it.

Usage:
    adk web --memory_service_uri finsight:// src/orchestrator/web
"""

import logging

from google.adk.cli.service_registry import get_service_registry

from shared.memory.memory_service import SQLiteMemoryService

logger = logging.getLogger(__name__)

registry = get_service_registry()


def finsight_memory_factory(uri: str, **kwargs: object) -> SQLiteMemoryService:
    """Create a SQLiteMemoryService instance."""
    logger.info("Creating SQLiteMemoryService for URI: %s", uri)
    return SQLiteMemoryService()


registry.register_memory_service("finsight", finsight_memory_factory)
logger.info("Registered finsight:// memory service")
