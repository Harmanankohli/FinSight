from abc import ABC
from collections.abc import AsyncIterable

from pydantic import BaseModel, Field


class BaseAgent(BaseModel, ABC):
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
        ...
