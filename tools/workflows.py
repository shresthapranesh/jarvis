"""Agent tools for managing workflows via the Jarvis database."""
from __future__ import annotations

import json

from db.engine import async_session
from db.ops import (
    create_workflow as _create,
    delete_workflow as _delete,
    list_workflows as _list,
    update_workflow as _update,
)


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
    """Delete a workflow by its id."""
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
