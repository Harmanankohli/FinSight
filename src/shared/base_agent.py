"""Abstract base for all FinSight A2A sub-agents.

Defines the agent interface contract — lifecycle (stream), telemetry, and
structured response helpers.
"""
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class BaseAgent(BaseModel, ABC):
    """Abstract base for all FinSight A2A sub-agents.

    Subclasses implement ``stream()`` — an async generator yielding status
    dicts and a final result.  GenericAgentExecutor wraps this in the A2A
    protocol (TaskStatusUpdateEvent / TaskArtifactUpdateEvent).
    """

    model_config = {
        "arbitrary_types_allowed": True,
        "extra": "allow",
    }

    agent_name: str = Field(description="The name of the agent")
    description: str = Field(description="A brief description of the agent's purpose")
    content_types: list[str] = Field(description="Supported content types")

    @asynccontextmanager
    async def _telemetry_span(
        self, span_name: str, query: str
    ) -> AsyncIterator[tuple[dict[str, str] | None, Any, str | None]]:
        """Context manager wrapping agent execution in a Langfuse span.

        Extracts trace IDs from the query, creates a child span, and
        yields (trace_ctx, span, trace_id) for downstream correlation.
        """
        from shared.observability import get_langfuse_client
        from shared.trace_context import extract_trace_ids

        trace_id, parent_span_id, query = extract_trace_ids(query)
        langfuse = get_langfuse_client()
        trace_ctx = (
            {"trace_id": trace_id, "parent_span_id": parent_span_id}
            if trace_id and parent_span_id
            else None
        )
        with langfuse.start_as_current_observation(
            as_type="span",
            name=span_name,
            input=query,
            trace_context=trace_ctx,
        ) as span:
            if trace_id:
                trace_ctx = {"trace_id": trace_id, "parent_span_id": span.id}
            yield trace_ctx, span, trace_id

    def _error_response(self, message: str) -> dict[str, Any]:
        """Build a standard error result dict for the stream protocol."""
        return {
            "response_type": "text",
            "is_task_complete": True,
            "is_error": True,
            "require_user_input": False,
            "content": message,
        }

    def _data_response(self, data: Any) -> dict[str, Any]:
        """Build a standard success result dict for the stream protocol."""
        return {
            "response_type": "data",
            "is_task_complete": True,
            "is_error": False,
            "require_user_input": False,
            "content": data,
        }

    @abstractmethod
    async def stream(
        self, query: str, context_id: str, task_id: str
    ) -> AsyncIterable[dict[str, Any]]:
        """Execute the agent's core logic, yielding streaming results.

        Each yielded dict contains:
          content            – partial text or structured data
          is_task_complete   – True on final yield
          is_error           – True if the agent failed
          require_user_input – True if the agent needs more info
        """
        yield {}  # makes this an async generator for type-checking
