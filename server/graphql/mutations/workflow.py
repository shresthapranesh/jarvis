"""Workflow mutations — create, update, delete, run, stop run."""

from __future__ import annotations

import json

import strawberry
from strawberry import relay
from strawberry.scalars import JSON

from core.state import _tasks
from db.ops import (
    create_workflow as db_create_workflow,
    delete_workflow as db_delete_workflow,
    get_workflow,
    update_workflow as db_update_workflow,
)

from ..types.workflow import Workflow
from server.workflow_runtime import register_workflow_run


@strawberry.input
class WorkflowCreateInput:
    name: str
    description: str | None = None
    definition: str = "{}"  # JSON string
    notifications: str | None = None  # JSON string


@strawberry.input
class WorkflowUpdateInput:
    name: str | None = None
    description: str | None = None
    definition: str | None = None  # JSON string
    notifications: str | None = None  # JSON string


@strawberry.type
class WorkflowMutation:
    @strawberry.mutation
    async def create_workflow(
        self,
        info: strawberry.Info,
        input: WorkflowCreateInput,
    ) -> Workflow:
        session = info.context["session"]
        wf = await db_create_workflow(
            session,
            name=input.name,
            description=input.description,
            definition=input.definition,
            notifications=input.notifications,
        )
        return Workflow.from_db(wf)

    @strawberry.mutation
    async def update_workflow(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        input: WorkflowUpdateInput,
    ) -> Workflow:
        session = info.context["session"]
        # Match REST: if no fields, return current row unchanged
        updates = {
            k: v for k, v in {
                "name": input.name,
                "description": input.description,
                "definition": input.definition,
                "notifications": input.notifications,
            }.items() if v is not None
        }
        if not updates:
            wf = await get_workflow(session, id.node_id)
            if wf is None:
                raise ValueError("workflow not found")
            return Workflow.from_db(wf)
        wf = await db_update_workflow(session, id.node_id, **updates)
        if wf is None:
            raise ValueError("workflow not found")
        return Workflow.from_db(wf)

    @strawberry.mutation
    async def delete_workflow(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> bool:
        session = info.context["session"]
        deleted = await db_delete_workflow(session, id.node_id)
        if not deleted:
            raise ValueError("workflow not found")
        return True

    @strawberry.mutation
    async def run_workflow(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        inputs: JSON | None = None,
    ) -> str:
        """Trigger a workflow run. `inputs` is a JSON object of node defaults.
        Returns run_id; client subscribes to workflowRunEvents(runId)."""
        session = info.context["session"]
        inputs_dict: dict = inputs if isinstance(inputs, dict) else {}
        run_id = await register_workflow_run(session, id.node_id, inputs_dict)
        if run_id is None:
            raise ValueError("workflow not found")
        return run_id

    @strawberry.mutation
    async def stop_workflow_run(self, run_id: str) -> bool:
        state = _tasks.get(run_id)
        if state is None:
            raise ValueError("run not found or already finished")
        if state.done:
            raise ValueError("run already finished")
        state.cancelled = True
        state._stop_event.set()
        from core.state import get_queue
        await get_queue().cancel(run_id)
        return True

    @strawberry.mutation
    async def resume_workflow_run(self, run_id: str, answer: str) -> bool:
        """Resume a workflow paused at an approval/human_input node.

        The answer is delivered to the pending node: for approval nodes,
        "approve"/"yes" or "deny"/"no" are interpreted; for human_input nodes,
        any free text is the answer.
        """
        from core.state import emit_event

        state = _tasks.get(run_id)
        if state is None:
            raise ValueError("run not found or not running")
        if state.resume_future is None or state.resume_future.done():
            raise ValueError("no pending human input for this run")
        pending_id = state.pending_interrupt_id
        state.resume_future.set_result(answer)
        emit_event(state, "interrupt_resolved", interrupt_id=pending_id)
        return True

    @strawberry.mutation
    async def resolve_workflow_approval(
        self,
        run_id: str,
        approved: bool,
        answer: str | None = None,
    ) -> bool:
        """Resolve a pending approval node with an explicit approved bool."""
        from core.state import emit_event

        state = _tasks.get(run_id)
        if state is None:
            raise ValueError("run not found or not running")
        if state.resume_future is None or state.resume_future.done():
            raise ValueError("no pending approval for this run")
        pending_id = state.pending_interrupt_id
        # Deliver as dict so ApprovalNode can distinguish
        payload = {"approved": approved, "answer": answer or ("approved" if approved else "denied")}
        state.resume_future.set_result(payload)
        emit_event(state, "interrupt_resolved", interrupt_id=pending_id)
        return True
