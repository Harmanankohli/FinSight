"""Register custom memory service for FinSight.

This file is automatically loaded by ADK when `adk web` starts.
It registers our SQLiteMemoryService under the 'finsight' URI scheme.

Usage:
    adk web --memory_service_uri finsight:// agents
"""

import logging
import sys
from pathlib import Path

# Ensure project root is on sys.path so 'shared' is importable
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from google.adk.cli.service_registry import get_service_registry
from shared.memory import SQLiteMemoryService

logger = logging.getLogger(__name__)

registry = get_service_registry()


# Factory for the URI-based memory service registration pattern (maps `finsight://` URIs to SQLiteMemoryService)
def finsight_memory_factory(uri: str, **kwargs) -> SQLiteMemoryService:
    """Create a SQLiteMemoryService instance."""
    logger.info("Creating SQLiteMemoryService for URI: %s", uri)
    return SQLiteMemoryService()


registry.register_memory_service("finsight", finsight_memory_factory)
logger.info("Registered finsight:// memory service")
