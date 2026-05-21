"""Workflow + WorkflowRun GraphQL types."""

from __future__ import annotations

from datetime import datetime

import strawberry
from strawberry import relay

from db import models as db_models
from db.ops import get_workflow, get_workflow_run


@strawberry.type
class Workflow(relay.Node):
    id: relay.NodeID[str]
    name: str
    description: str | None
    definition: str  # JSON string
    notifications: str | None  # JSON string
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_db(cls, row: db_models.Workflow) -> Workflow:
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            definition=row.definition,
            notifications=row.notifications,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @classmethod
    async def resolve_node(
        cls,
        node_id: str,
        *,
        info: strawberry.Info,
        required: bool = False,
    ) -> Workflow | None:
        session = info.context["session"]
        row = await get_workflow(session, node_id)
        if row is None:
            if required:
                raise ValueError(f"Workflow {node_id} not found")
            return None
        return cls.from_db(row)


@strawberry.type
class WorkflowRun(relay.Node):
    id: relay.NodeID[str]
    workflow_id: str
    status: str  # "running" | "done" | "error" | "stopped"
    inputs: str | None       # JSON string
    outputs: str | None      # JSON string
    node_results: str | None # JSON string
    error: str | None
    started_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_db(cls, row: db_models.WorkflowRun) -> WorkflowRun:
        return cls(
            id=row.id,
            workflow_id=row.workflow_id,
            status=row.status,
            inputs=row.inputs,
            outputs=row.outputs,
            node_results=row.node_results,
            error=row.error,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )

    @classmethod
    async def resolve_node(
        cls,
        node_id: str,
        *,
        info: strawberry.Info,
        required: bool = False,
    ) -> WorkflowRun | None:
        session = info.context["session"]
        row = await get_workflow_run(session, node_id)
        if row is None:
            if required:
                raise ValueError(f"WorkflowRun {node_id} not found")
            return None
        return cls.from_db(row)
