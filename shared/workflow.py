from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Status(str, Enum):
    PENDING = "pending"
    WORKING = "working"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowNode(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task: str = Field(description="The task description")
    node_key: str | None = None
    node_label: str | None = None
    status: Status = Status.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None


class WorkflowGraph(BaseModel):
    nodes: dict[str, WorkflowNode] = Field(default_factory=dict)
    edges: dict[str, list[str]] = Field(default_factory=dict)
    state: Status = Status.PENDING
    paused_node_id: str | None = None
    attributes: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def add_node(self, node: WorkflowNode) -> None:
        self.nodes[node.id] = node
        if node.id not in self.edges:
            self.edges[node.id] = []

    def add_edge(self, from_id: str, to_id: str) -> None:
        if from_id not in self.edges:
            self.edges[from_id] = []
        self.edges[from_id].append(to_id)

    def set_node_attributes(self, node_id: str, **kwargs) -> None:
        if node_id not in self.attributes:
            self.attributes[node_id] = {}
        self.attributes[node_id].update(kwargs)

    def get_node_attributes(self, node_id: str) -> dict[str, Any]:
        return self.attributes.get(node_id, {})

    def get_next_nodes(self, node_id: str) -> list[WorkflowNode]:
        next_ids = self.edges.get(node_id, [])
        return [self.nodes[nid] for nid in next_ids if nid in self.nodes]

    def mark_completed(self, node_id: str, result: dict | None = None) -> None:
        if node_id in self.nodes:
            self.nodes[node_id].status = Status.COMPLETED
            self.nodes[node_id].result = result

    def mark_failed(self, node_id: str, error: str) -> None:
        if node_id in self.nodes:
            self.nodes[node_id].status = Status.FAILED
            self.nodes[node_id].error = error

    async def run_workflow(self, start_node_id: str) -> None:
        self.state = Status.WORKING
        current_id = start_node_id
        while current_id:
            node = self.nodes.get(current_id)
            if not node:
                break
            node.status = Status.WORKING
            next_nodes = self.get_next_nodes(current_id)
            if not next_nodes:
                self.state = Status.COMPLETED
                break
            current_id = next_nodes[0].id
