"""Workflow execution engine.

Traverses a workflow graph, executes nodes in dependency order, and
streams SSE events through a TaskState. Supports conditional branching
via ConditionalNode's ``next_handles`` — inactive branches are pruned
and their nodes never execute.

Graph definition format (stored as JSON in ``Workflow.definition``):

    {
        "nodes": [
            {
                "id": "n1",
                "type": "agent",
                "label": "Research",
                "config": {
                    "prompt_template": "Research: {{topic}}",
                    "model": "google_genai:gemini-2.0-flash",
                    "output_key": "research",
                    "input_ports": ["topic"]
                }
            },
            {
                "id": "n2",
                "type": "conditional",
                "label": "Good enough?",
                "config": {
                    "condition": "Is the following comprehensive? {{research}}",
                    "model": "google_genai:gemini-2.0-flash",
                    "input_key": "research"
                }
            }
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n1",
                "sourceHandle": "research",
                "target": "n2",
                "targetHandle": "research"
            }
        ]
    }

SSE events emitted:
    node_start:     {node_id, node_type, label}
    node_token:     {node_id, text}           — streaming from AgentNode
    node_condition: {node_id, verdict}        — "true" | "false"
    node_done:      {node_id, output}
    node_error:     {node_id, error}
    workflow_done:  {outputs, run_id}
    workflow_error: {error, run_id}
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Any

from core.state import TaskState
from workflow.nodes import build_node, _emit, _interpolate


# ── Graph helpers ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_start_nodes(nodes_by_id: dict[str, dict], edges: list[dict]) -> list[str]:
    """Return IDs of nodes that have no incoming edges (entry points)."""
    has_incoming = {e["target"] for e in edges}
    return [nid for nid in nodes_by_id if nid not in has_incoming]


def _is_ready(
    node_id: str,
    edges: list[dict],
    completed: dict[str, dict],
    pruned_edges: set[str],
) -> bool:
    """A node is ready when all its active incoming edges come from completed nodes.

    Active incoming edge = an edge targeting this node whose id is not pruned.
    If every incoming edge is pruned the node is unreachable and never ready.
    If there are no incoming edges the node is a start node and always ready
    (caller should have already enqueued it, but this handles re-checks safely).
    """
    all_incoming = [e for e in edges if e["target"] == node_id]
    active_incoming = [e for e in all_incoming if e["id"] not in pruned_edges]

    if all_incoming and not active_incoming:
        # All paths to this node are pruned — unreachable
        return False

    return all(e["source"] in completed for e in active_incoming)


def _resolve_node_inputs(
    node_id: str,
    edges: list[dict],
    completed: dict[str, dict],
    pruned_edges: set[str],
) -> dict[str, Any]:
    """Build the inputs dict for a node by following active incoming edges.

    Each active incoming edge maps ``sourceHandle`` from the source node's
    output to ``targetHandle`` in this node's input namespace.
    """
    inputs: dict[str, Any] = {}
    for edge in edges:
        if edge["target"] != node_id:
            continue
        if edge["id"] in pruned_edges:
            continue
        source_id = edge["source"]
        source_handle = edge.get("sourceHandle", "")
        target_handle = edge.get("targetHandle", source_handle)
        source_outputs = completed.get(source_id, {})
        if source_handle in source_outputs:
            inputs[str(target_handle)] = source_outputs[source_handle]
    return inputs


# ── Execution engine ──────────────────────────────────────────────────────────

async def execute_workflow(
    run_id: str,
    definition: dict,
    inputs: dict[str, Any],
    task_state: TaskState,
) -> tuple[dict[str, Any], list[dict]]:
    """Execute a workflow graph end-to-end.

    Args:
        run_id:      WorkflowRun id (embedded in workflow_done/workflow_error events)
        definition:  parsed graph dict with ``nodes`` and ``edges`` lists
        inputs:      external inputs keyed by port name; merged into the
                     virtual outputs of a synthetic start context so that
                     entry nodes can receive them via edges — OR just passed
                     directly as the inputs dict for nodes with no incoming edges
        task_state:  live TaskState for SSE emission

    Returns:
        dict of outputs from all terminal nodes (nodes with no active outgoing edges)

    Raises:
        ValueError: if the definition is malformed (missing nodes/edges keys)
    """
    raw_nodes: list[dict] = definition.get("nodes", [])
    edges: list[dict] = definition.get("edges", [])

    if not raw_nodes:
        _emit(task_state, "workflow_error", error="workflow has no nodes", run_id=run_id)
        return {}, []

    nodes_by_id: dict[str, dict] = {n["id"]: n for n in raw_nodes}

    # completed[node_id] = {output_port: value}
    completed: dict[str, dict] = {}
    # Seed completed with the workflow-level inputs so edge-connected entry nodes
    # can receive them.  We use a virtual node id "__inputs__" and create edges
    # from it to the first real nodes if they declare no incoming edges.
    # Simpler approach: entry nodes (no incoming edges) receive `inputs` directly.

    pruned_edges: set[str] = set()
    executed: set[str] = set()

    # Successors index: node_id → list of downstream node ids (used when
    # enqueuing newly-unblocked nodes after a completion — O(n+m) vs O(n²))
    successors: dict[str, list[str]] = {nid: [] for nid in nodes_by_id}
    for edge in edges:
        successors[edge["source"]].append(edge["target"])

    # Initialise queue with start nodes
    queue: deque[str] = deque(_get_start_nodes(nodes_by_id, edges))

    # Per-node result records accumulated for DB persistence (returned separately)
    node_records: list[dict] = []

    while queue:
        if task_state.cancelled:
            raise asyncio.CancelledError()

        node_id = queue.popleft()

        if node_id in executed:
            continue

        node_def = nodes_by_id[node_id]
        node_type = node_def.get("type", "")
        label = node_def.get("label", node_id)
        config = node_def.get("config", {})

        # Resolve inputs: from edges for non-start nodes; from workflow inputs for start nodes
        node_inputs = _resolve_node_inputs(node_id, edges, completed, pruned_edges)
        if not node_inputs:
            # Start node — inject workflow-level inputs
            node_inputs = dict(inputs)

        _emit(task_state, "node_start", node_id=node_id, node_type=node_type, label=label)

        record: dict = {
            "node_id": node_id,
            "node_type": node_type,
            "label": label,
            "status": "running",
            "inputs": node_inputs,
            "outputs": None,
            "error": None,
            "started_at": _now_iso(),
            "finished_at": None,
        }
        if node_type == "agent":
            record["rendered_prompt"] = _interpolate(
                config.get("prompt_template", ""), node_inputs
            )

        try:
            node = build_node(node_id, node_type, config)
            result = await node.execute(node_inputs, task_state)
        except Exception as exc:
            error_msg = str(exc)
            record["status"] = "error"
            record["error"] = error_msg
            record["finished_at"] = _now_iso()
            node_records.append(record)

            _emit(task_state, "node_error", node_id=node_id, error=error_msg)
            # Continue executing other independent branches
            executed.add(node_id)
            continue

        executed.add(node_id)
        completed[node_id] = result.data

        record["status"] = "done"
        record["outputs"] = result.data
        record["finished_at"] = _now_iso()
        if result.next_handles is not None:
            record["verdict"] = result.next_handles[0]  # "true" or "false"
        node_records.append(record)

        _emit(task_state, "node_done", node_id=node_id, output=result.data)

        # Handle conditional pruning
        if result.next_handles is not None:
            for edge in edges:
                if edge["source"] != node_id:
                    continue
                if edge.get("sourceHandle") not in result.next_handles:
                    pruned_edges.add(edge["id"])

        # Enqueue newly unblocked downstream nodes
        for candidate_id in successors.get(node_id, []):
            if candidate_id not in executed and candidate_id not in queue:
                if _is_ready(candidate_id, edges, completed, pruned_edges):
                    queue.append(candidate_id)

    # Collect outputs from terminal nodes (no active outgoing edges)
    final_outputs: dict[str, Any] = {}
    for node_id, node_outputs in completed.items():
        active_outgoing = [
            e for e in edges
            if e["source"] == node_id and e["id"] not in pruned_edges
        ]
        if not active_outgoing:
            final_outputs.update(node_outputs)

    _emit(task_state, "workflow_done", outputs=final_outputs, run_id=run_id)

    return final_outputs, node_records
