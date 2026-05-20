"""Register custom memory service for FinSight.

This file is automatically loaded by ADK when `adk web` starts.
It registers our SQLiteMemoryService under the 'finsight' URI scheme.

Usage:
    adk web --memory_service_uri finsight:// agents
"""

from google.adk.cli.service_registry import get_service_registry
from shared.memory import SQLiteMemoryService

registry = get_service_registry()


def finsight_memory_factory(uri: str, **kwargs) -> SQLiteMemoryService:
    """Create a SQLiteMemoryService instance."""
    return SQLiteMemoryService()


registry.register_memory_service("finsight", finsight_memory_factory)
