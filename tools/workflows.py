"""Agent tools for managing workflows via the Jarvis database.

Includes CRUD (manage_workflows) plus Agent-as-Tool execution (run_workflow)
which lets the main agent invoke a saved workflow as a sub-agent — ADK's
AgentTool pattern.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import contextvars

from langchain_core.tools import tool

from db.engine import async_session
from db.ops import (
    create_workflow as _create,
    delete_workflow as _delete,
    get_workflow as _get,
    list_workflows as _list,
    update_workflow as _update,
)
from tools.context import current_ctx

# Recursion guard for Agent-as-Tool: workflow AgentNode builds the same
# main agent (which includes run_workflow), so a workflow containing an agent
# node could call run_workflow on itself forever. Track depth via ContextVar.
_workflow_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "jarvis_workflow_depth", default=0
)
_MAX_WORKFLOW_DEPTH = 3


async def list_workflows() -> str:
    """List all saved workflows with their id, name, and description."""
    async with async_session() as session:
        workflows = await _list(session)
    if not workflows:
        return "No workflows found."
    return "\n".join(
        f"- id={wf.id} | {wf.name}" + (f" — {wf.description}" if wf.description else "")
        for wf in workflows
    )


async def create_workflow(
    name: str,
    definition: str,
    description: str | None = None,
) -> str:
    """Create a new workflow.

    Args:
        name: Human-readable workflow name.
        description: Optional short description.
        definition: JSON string describing the graph. Structure:

            {
              "nodes": [
                {"id": "n1", "type": "start", "label": "Start",
                 "position": {"x": 100, "y": 100},
                 "config": {"initial_inputs": {"topic": ""}}},

                {"id": "n2", "type": "agent", "label": "Research",
                 "position": {"x": 300, "y": 250},
                 "config": {
                   "prompt_template": "Research {{topic}} thoroughly.",
                   "model": "google_genai:gemini-2.0-flash",
                   "output_key": "research"
                 }},

                {"id": "n3", "type": "conditional", "label": "Good enough?",
                 "position": {"x": 500, "y": 400},
                 "config": {
                   "condition": "Is this research comprehensive? {{research}}",
                   "model": "google_genai:gemini-2.0-flash",
                   "input_key": "research"
                 }}
              ],
              "edges": [
                {"id": "e1", "source": "n1", "sourceHandle": "topic",
                 "target": "n2", "targetHandle": "topic"},
                {"id": "e2", "source": "n2", "sourceHandle": "research",
                 "target": "n3", "targetHandle": "research"}
              ]
            }

            Node types:
              - "start"       : entry point; config.initial_inputs = {key: default_value}
              - "agent"       : LLM step; prompt_template uses {{var}}; output_key names its output port
              - "conditional" : yes/no LLM check; has "true" and "false" output handles

            Edges: sourceHandle (output port name) -> targetHandle (input port name).
            Use unique string ids for all nodes and edges.
    """
    try:
        json.loads(definition)
    except json.JSONDecodeError as e:
        return f"Invalid definition JSON: {e}"

    async with async_session() as session:
        wf = await _create(session, name=name, description=description, definition=definition)
    return f"Created workflow '{wf.name}' (id={wf.id})."


async def update_workflow(workflow_id: str, **fields) -> str:
    """Update fields on an existing workflow (name, description, definition)."""
    async with async_session() as session:
        wf = await _update(session, workflow_id, **fields)
    if wf is None:
        return f"Workflow '{workflow_id}' not found."
    return f"Updated workflow '{wf.name}' (id={wf.id})."


async def delete_workflow(workflow_id: str) -> str:
    """Delete a workflow by its id. Requires approval."""
    from core.approval import request_tool_approval

    if not request_tool_approval(
        "delete_workflow",
        {"workflow_id": workflow_id},
        f"Delete workflow {workflow_id}? This cannot be undone.",
    ):
        return "User denied approval — not deleting workflow."

    async with async_session() as session:
        deleted = await _delete(session, workflow_id)
    return "Deleted." if deleted else f"Workflow '{workflow_id}' not found."


async def manage_workflows(
    action: str,
    workflow_id: str | None = None,
    name: str | None = None,
    description: str | None = None,
    definition: str | None = None,
) -> str:
    """List, create, update, or delete saved workflows (multi-step node graphs).

    For update, pass workflow_id plus only the fields to change.

    Args:
        action: One of "list", "create", "update", "delete".
        workflow_id: Target workflow id (required for update/delete).
        name: Human-readable workflow name (required for create).
        description: Optional short description.
        definition: JSON string describing the graph (required for create).
            Structure:

            {"nodes": [
               {"id": "n1", "type": "start", "label": "Start",
                "position": {"x": 100, "y": 100},
                "config": {"initial_inputs": {"topic": ""}}},
               {"id": "n2", "type": "agent", "label": "Research",
                "position": {"x": 300, "y": 250},
                "config": {"prompt_template": "Research {{topic}} thoroughly.",
                           "model": "google_genai:gemini-2.0-flash",
                           "output_key": "research"}},
               {"id": "n3", "type": "conditional", "label": "Good enough?",
                "position": {"x": 500, "y": 400},
                "config": {"condition": "Is this comprehensive? {{research}}",
                           "model": "google_genai:gemini-2.0-flash",
                           "input_key": "research"}}],
             "edges": [
               {"id": "e1", "source": "n1", "sourceHandle": "topic",
                "target": "n2", "targetHandle": "topic"},
               {"id": "e2", "source": "n2", "sourceHandle": "research",
                "target": "n3", "targetHandle": "research"}]}

            Node types: "start" (entry point; config.initial_inputs =
            {key: default}), "agent" (LLM step; prompt_template uses {{var}};
            output_key names its output port), "conditional" (yes/no LLM check
            with "true"/"false" output handles). Edges connect sourceHandle
            (output port) to targetHandle (input port). Use unique string ids
            for all nodes and edges.
    """
    action = (action or "").strip().lower()
    if action == "list":
        return await list_workflows()
    if action == "create":
        if not name or not definition:
            return "Error: create requires name and definition."
        return await create_workflow(name=name, definition=definition, description=description)
    if action == "update":
        if not workflow_id:
            return "Error: update requires workflow_id."
        if definition is not None:
            try:
                json.loads(definition)
            except json.JSONDecodeError as e:
                return f"Invalid definition JSON: {e}"
        fields = {
            k: v
            for k, v in dict(name=name, description=description, definition=definition).items()
            if v is not None
        }
        if not fields:
            return "Error: update requires at least one field to change."
        return await update_workflow(workflow_id, **fields)
    if action == "delete":
        if not workflow_id:
            return "Error: delete requires workflow_id."
        return await delete_workflow(workflow_id)
    return f"Error: unknown action '{action}'; must be one of list, create, update, delete."


# ── Agent-as-Tool: run_workflow ────────────────────────────────────────────

@tool
async def run_workflow(workflow_id: str, inputs_json: str | None = None) -> str:
    """Execute a saved workflow as a sub-agent (ADK Agent-as-Tool pattern).

    Loads the workflow by id, runs it with the given inputs, and returns its
    final outputs as JSON. Use this to delegate multi-step work to a reusable
    workflow you or the user built.

    Args:
        workflow_id: ID of the workflow to run (from list_workflows / manage_workflows).
        inputs_json: Optional JSON object string with input key-values for the
            workflow's start nodes, e.g. '{"topic": "AI news"}'. Defaults to {}.

    Returns:
        JSON string of the workflow's terminal outputs, or an error message.
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
