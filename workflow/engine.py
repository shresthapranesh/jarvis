"""Workflow execution engine.

Traverses a workflow graph in dependency order, running each ready frontier
of mutually-independent nodes concurrently, and streams SSE events through a
TaskState. Supports conditional branching via ConditionalNode's
``next_handles`` — inactive branches are pruned and their nodes never execute.

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

Per-node resilience (new):
    config options:
      timeout_seconds (float): max wall-clock for this node
      retries (int, default 0): number of retry attempts after failure
      retry_delay_seconds (float, default 1): sleep between retries
      on_error (str, default "error"): "error" (branch stalls) or "continue"
        (emit done with fallback_output or {} and keep branch)
      fallback_output (dict): data to return when on_error="continue" after all
        retries exhausted; merged under output ports if provided

Expression language (new):
    prompt_template / condition / instruction / reason / rubric etc now use
    Jinja2 if installed, else regex fallback:
      {{var}} -> same as {{inputs.var}} (legacy direct)
      {{inputs.foo}}
      {{nodes.node_id.port}} or {{nodes.node_id}} (whole output dict as JSON string in fallback)
      {{workflow.foo}} -> top-level workflow inputs
    Filters when Jinja available: upper, lower, trim, default, tojson/json, fromjson

SSE events emitted:
    node_start:     {node_id, node_type, label}
    node_token:     {node_id, text}           — streaming from AgentNode
    node_condition: {node_id, verdict}        — "true" | "false"
    node_retry:     {node_id, attempt, max_retries, error} — emitted between retries
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
from core.workflow_template import (
    render_template,
    reset_template_context,
    set_template_context,
)
from workflow.nodes import NodeOutput, build_node, _emit, _interpolate


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

    async def _run_node(node_id: str) -> tuple[str, NodeOutput | None, dict]:
        """Execute one node with retry/timeout/on_error resilience.

        Returns ``(node_id, result, record)``; ``result`` is None when the node
        ultimately failed and on_error="error" — branch stalls; otherwise
        result may be a fallback NodeOutput when on_error="continue".
        """
        node_def = nodes_by_id[node_id]
        node_type = node_def.get("type", "")
        label = node_def.get("label", node_id)
        config = node_def.get("config", {})

        # Resolve inputs
        node_inputs = _resolve_node_inputs(node_id, edges, completed, pruned_edges)
        if not node_inputs:
            node_inputs = dict(inputs)

        # Resilience knobs
        timeout_raw = config.get("timeout_seconds", config.get("timeout"))
        try:
            timeout_val: float | None = float(timeout_raw) if timeout_raw is not None else None
            if timeout_val is not None and timeout_val <= 0:
                timeout_val = None
        except Exception:
            timeout_val = None

        try:
            retries = max(0, int(config.get("retries", 0)))
        except Exception:
            retries = 0
        retries = min(retries, 10)

        try:
            retry_delay = float(config.get("retry_delay_seconds", config.get("retry_delay", 1.0)))
        except Exception:
            retry_delay = 1.0
        retry_delay = max(0.0, min(retry_delay, 60.0))

        on_error = str(config.get("on_error", "error")).lower()
        if on_error not in ("error", "continue", "skip"):
            on_error = "error"
        fallback_output = config.get("fallback_output")

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
            "attempts": 0,
            "timeout_seconds": timeout_val,
            "retries_config": retries,
            "on_error": on_error,
        }
        if node_type == "agent":
            try:
                rendered = render_template(
                    config.get("prompt_template", ""), node_inputs, completed, inputs
                )
            except Exception:
                rendered = _interpolate(config.get("prompt_template", ""), node_inputs, completed, inputs)
            record["rendered_prompt"] = rendered

        # Set template context var for {{nodes.*}} resolution inside node.execute
        ctx_token = set_template_context(completed, inputs)

        last_error: str | None = None
        last_exc: Exception | None = None

        try:
            for attempt in range(retries + 1):
                if task_state.cancelled:
                    raise asyncio.CancelledError()

                record["attempts"] = attempt + 1
                try:
                    node = build_node(node_id, node_type, config)
                    if timeout_val is not None:
                        result = await asyncio.wait_for(
                            node.execute(node_inputs, task_state), timeout=timeout_val
                        )
                    else:
                        result = await node.execute(node_inputs, task_state)

                    # Success
                    record["status"] = "done"
                    record["outputs"] = result.data
                    record["finished_at"] = _now_iso()
                    if result.next_handles is not None:
                        record["verdict"] = result.next_handles[0]
                    _emit(task_state, "node_done", node_id=node_id, output=result.data)
                    return node_id, result, record

                except asyncio.TimeoutError as exc:
                    last_exc = exc
                    last_error = f"timeout after {timeout_val}s"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_exc = exc
                    last_error = str(exc)

                # Failure handling for this attempt
                if attempt < retries:
                    _emit(
                        task_state,
                        "node_retry",
                        node_id=node_id,
                        attempt=attempt + 1,
                        max_retries=retries,
                        error=last_error,
                    )
                    # Respect cancellation during sleep
                    try:
                        await asyncio.wait_for(
                            asyncio.sleep(retry_delay), timeout=retry_delay + 1
                        )
                    except asyncio.TimeoutError:
                        pass
                    if task_state.cancelled:
                        raise asyncio.CancelledError()
                    continue
                else:
                    # Exhausted retries
                    break

            # All attempts failed
            error_msg = last_error or "unknown error"
            record["status"] = "error"
            record["error"] = error_msg
            record["finished_at"] = _now_iso()
            _emit(task_state, "node_error", node_id=node_id, error=error_msg)

            if on_error in ("continue", "skip"):
                # Produce fallback output so branch continues
                if isinstance(fallback_output, dict):
                    fallback_data = fallback_output
                else:
                    fallback_data = {}
                # If fallback empty and we have an output_key hint, produce empty under that key? Keep empty dict.
                fallback_result = NodeOutput(data=fallback_data)
                record["status"] = "done"
                record["outputs"] = fallback_result.data
                record["error"] = error_msg  # keep error for visibility but status done
                record["fallback_used"] = True
                _emit(task_state, "node_done", node_id=node_id, output=fallback_result.data)
                return node_id, fallback_result, record

            return node_id, None, record

        except asyncio.CancelledError:
            record["status"] = "error"
            record["error"] = "cancelled"
            record["finished_at"] = _now_iso()
            _emit(task_state, "node_error", node_id=node_id, error="cancelled")
            return node_id, None, record
        finally:
            reset_template_context(ctx_token)

    # Level-synchronized BFS: each iteration drains the entire ready frontier
    # and runs those nodes concurrently. They are independent by construction —
    # a node only becomes ready once all its active predecessors have completed,
    # so no two nodes in one frontier can depend on each other. The barrier
    # between levels keeps `completed`/`pruned_edges` consistent before the next
    # frontier's readiness (and any conditional pruning) is computed.
    while queue:
        if task_state.cancelled:
            raise asyncio.CancelledError()

        # Drain the current frontier, skipping already-run / duplicate ids.
        frontier: list[str] = []
        batch_seen: set[str] = set()
        while queue:
            nid = queue.popleft()
            if nid in executed or nid in batch_seen:
                continue
            frontier.append(nid)
            batch_seen.add(nid)

        if not frontier:
            break

        batch = await asyncio.gather(*(_run_node(nid) for nid in frontier))

        if task_state.cancelled:
            raise asyncio.CancelledError()

        # Apply results in frontier order so node_records and final outputs are
        # deterministic regardless of which node's coroutine finished first.
        for node_id, result, record in batch:
            executed.add(node_id)
            node_records.append(record)
            if result is None:
                continue  # errored — its branch stalls; others continue
            completed[node_id] = result.data
            # Handle conditional pruning
            if result.next_handles is not None:
                for edge in edges:
                    if edge["source"] != node_id:
                        continue
                    if edge.get("sourceHandle") not in result.next_handles:
                        pruned_edges.add(edge["id"])

        # With the whole level applied, enqueue every newly-unblocked successor.
        for node_id, result, _ in batch:
            if result is None:
                continue
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
