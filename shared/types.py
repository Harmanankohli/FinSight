from typing import Any, Literal

from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str
    port: int
    transport: str
    url: str


class PlannerTask(BaseModel):
    id: int = Field(description="Sequential ID for the task")
    description: str = Field(description="Clear description of the task")
    skill_id: str = Field(description="The skill ID required for this task")
    params: dict[str, Any] = Field(default_factory=dict, description="Parameters for the task")
    status: Literal["pending", "working", "completed", "failed", "input_required"] | None = "pending"


class TaskList(BaseModel):
    original_query: str | None = Field(description="The original user query")
    tasks: list[PlannerTask] = Field(description="Ordered list of tasks to execute")


class AgentResponse(BaseModel):
    content: str | dict = Field(description="The content of the response")
    is_task_complete: bool = Field(description="Whether the task is complete")
    require_user_input: bool = Field(description="Whether agent requires user input")
