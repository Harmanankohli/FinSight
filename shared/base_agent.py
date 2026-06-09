import logging
from abc import ABC
from collections.abc import AsyncIterable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class BaseAgent(BaseModel, ABC):
    """Abstract base for all FinSight A2A sub-agents (RAG, Quant, Market Context).

    Each concrete subclass implements ``stream()`` — an async generator that
    yields streaming status dicts and a final result.  GenericAgentExecutor
    wraps this in the A2A protocol (TaskStatusUpdateEvent / TaskArtifactUpdateEvent).
    """

    model_config = {
        "arbitrary_types_allowed": True,
        "extra": "allow",
    }

    agent_name: str = Field(description="The name of the agent")
    description: str = Field(description="A brief description of the agent's purpose")
    content_types: list[str] = Field(description="Supported content types")

    async def stream(
        self, query: str, context_id: str, task_id: str
    ) -> AsyncIterable[dict[str, any]]:
        """Execute the agent's core logic and yield streaming results.

        Yields dicts with keys:
          content           – partial text or structured data
          is_task_complete  – True on final yield
          is_error          – True if agent failed
          require_user_input – True if agent needs more info
        """
        ...
