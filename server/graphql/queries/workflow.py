"""Workflow queries — list, single, runs, single run."""

from __future__ import annotations

import strawberry
from strawberry import relay

from db.ops import get_workflow, get_workflow_run, list_workflow_runs, list_workflows

from ..types.workflow import Workflow, WorkflowRun


@strawberry.type
class WorkflowQuery:
    @strawberry.field
    async def workflows(self, info: strawberry.Info) -> list[Workflow]:
        session = info.context["session"]
        rows = await list_workflows(session)
        return [Workflow.from_db(w) for w in rows]

    @strawberry.field
    async def workflow(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> Workflow | None:
        session = info.context["session"]
        row = await get_workflow(session, id.node_id)
        if row is None:
            return None
        return Workflow.from_db(row)

    @strawberry.field
    async def workflow_runs(
        self,
        info: strawberry.Info,
        workflow_id: relay.GlobalID,
    ) -> list[WorkflowRun]:
        session = info.context["session"]
        rows = await list_workflow_runs(session, workflow_id.node_id)
        return [WorkflowRun.from_db(r) for r in rows]

    @strawberry.field
    async def workflow_run(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> WorkflowRun | None:
        session = info.context["session"]
        row = await get_workflow_run(session, id.node_id)
        if row is None:
            return None
        return WorkflowRun.from_db(row)
