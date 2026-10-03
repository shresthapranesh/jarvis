"""`run_workflow`: the main agent invokes a saved workflow as a sub-agent —
ADK's AgentTool pattern. Workflow CRUD is in the `jarvis` SDK (`tools/sdk.py`).
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import contextvars

from langchain_core.tools import tool

from db.engine import async_session
from db.ops import get_workflow as _get
from tools.context import current_ctx

# Recursion guard for Agent-as-Tool: workflow AgentNode builds the same
# main agent (which includes run_workflow), so a workflow containing an agent
# node could call run_workflow on itself forever. Track depth via ContextVar.
_workflow_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "jarvis_workflow_depth", default=0
)
_MAX_WORKFLOW_DEPTH = 3


# ── Agent-as-Tool: run_workflow ────────────────────────────────────────────

@tool
async def run_workflow(workflow_id: str, inputs_json: str | None = None) -> str:
    """Run a saved workflow as a sub-agent; returns its final outputs as JSON.

    Args:
        workflow_id: ID of the workflow to run.
        inputs_json: JSON object of start-node inputs, e.g. '{"topic": "AI news"}'.
    """
    # Recursion guard: prevent workflow AgentNode -> run_workflow -> same workflow -> ...
    depth = _workflow_depth.get()
    if depth >= _MAX_WORKFLOW_DEPTH:
        return (
            f"Error: workflow recursion depth {depth} exceeds limit {_MAX_WORKFLOW_DEPTH} — "
            f"possible self-invocation loop for workflow {workflow_id!r}. Aborting."
        )
    token = _workflow_depth.set(depth + 1)

    # Parse inputs
    inputs: dict[str, Any] = {}
    if inputs_json:
        try:
            parsed = json.loads(inputs_json)
            if not isinstance(parsed, dict):
                return f"Error: inputs_json must be a JSON object, got {type(parsed).__name__}"
            inputs = parsed
        except json.JSONDecodeError as e:
            return f"Error: inputs_json is not valid JSON: {e}"

    # Load workflow definition
    async with async_session() as session:
        wf = await _get(session, workflow_id)
    if wf is None:
        return f"Workflow '{workflow_id}' not found."

    try:
        definition = json.loads(wf.definition)
    except Exception as e:
        return f"Failed to parse workflow definition: {e}"

    # Run via workflow engine with a temporary TaskState so node_token events
    # flow through the same emit mechanism. Forward key lifecycle events to the
    # parent agent's stream via current_ctx().emit for visibility.
    from core.state import TaskState
    from workflow.engine import execute_workflow

    tctx = current_ctx()
    run_id = f"tool_{workflow_id}_{uuid4().hex[:8]}"
    child_state = TaskState(label=f"workflow:{wf.name}", parent_id=run_id)

    # Use numeric idx for worker events — string idx crashes coerce_chat_event
    worker_idx = abs(hash(run_id)) % 9000 + 1000

    try:
        try:
            tctx.emit("worker_start", idx=worker_idx, role="workflow", task=f"Running workflow '{wf.name}'")
            outputs, _records = await execute_workflow(run_id, definition, inputs, child_state)
            tctx.emit(
                "worker_done",
                idx=worker_idx,
                role="workflow",
                task=f"Workflow '{wf.name}' done",
                status="done",
                result=json.dumps(outputs)[:2000],
            )
        except Exception as e:
            tctx.emit(
                "worker_done",
                idx=worker_idx,
                role="workflow",
                task=f"Workflow '{wf.name}' failed",
                status="error",
                result=str(e)[:1000],
            )
            return f"Workflow execution failed: {e}"

        try:
            return json.dumps(outputs, indent=2)
        except Exception:
            return str(outputs)
    finally:
        _workflow_depth.reset(token)
