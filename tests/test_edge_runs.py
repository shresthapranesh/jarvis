"""Run subscriptions (`edge/src/gql/events.rs`, `edge/src/runs.rs`): what a
subscriber gets, diffed against Python's schema, recorded
(`python_golden.py`).

Every event type goes through every subscription's coercer: Python emitted
the events with the real `emit_event`, and its raw records are replayed into
the edge (`jarvis-edge --replay-events`). A run that isn't live falls back to
its row, over both WebSocket protocols.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from edge_support import _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture
from python_golden import recorded

CHAT = """subscription($id: String!) { taskEvents(taskId: $id) { __typename
  ... on TokenEvent { text source } ... on ThinkingTokenEvent { text source }
  ... on StepEvent { node source subagent data } ... on BrowserStepEvent { url phase source }
  ... on WorkerStartEvent { idx role task } ... on WorkerStepEvent { idx role node data }
  ... on WorkerTokenEvent { idx text } ... on WorkerDoneEvent { idx role task status result }
  ... on ArtifactEvent { artifactId title action kind preview }
  ... on TodosUpdatedEvent { todos { text status } source }
  ... on QueuedMessageEvent { messageId text position } ... on QueuedWithdrawnEvent { messageId }
  ... on QueuedConsumedEvent { messageIds }
  ... on ApprovalRequestEvent { tool reason args approvalId deferred }
  ... on ApprovalResolvedEvent { tool approved answer }
  ... on WorkflowToolEvent { parentRunId childEvent data }
  ... on BudgetExceededEvent { reason snapshot }
  ... on BudgetUpdateEvent { inputTokens outputTokens totalTokens llmCalls toolCalls snapshot }
  ... on PerfUpdateEvent { ttftMs llmMs prefillTps evalTps llmCalls snapshot }
  ... on DoneEvent { message conversationId } ... on StoppedEvent { message conversationId }
  ... on ErrorEvent { error } } }"""

AUTOMATION = """subscription($id: String!) { automationRunEvents(runId: $id) { __typename
  ... on TokenEvent { text source } ... on AutomationDoneEvent { output runId }
  ... on AutomationStoppedEvent { output runId } ... on ErrorEvent { error } } }"""

BOARD = AUTOMATION.replace("automationRunEvents", "boardTaskEvents")

WORKFLOW = """subscription($id: String!) { workflowRunEvents(runId: $id) { __typename
  ... on WorkflowNodeStartEvent { nodeId nodeType label } ... on WorkflowNodeTokenEvent { nodeId text }
  ... on WorkflowNodeConditionEvent { nodeId verdict } ... on WorkflowNodeDoneEvent { nodeId output }
  ... on WorkflowNodeErrorEvent { nodeId error } ... on WorkflowMapStartEvent { nodeId total }
  ... on WorkflowMapItemDoneEvent { nodeId index result }
  ... on WorkflowApprovalRequestEvent { tool reason args nodeId }
  ... on WorkflowApprovalResolvedEvent { tool approved answer nodeId }
  ... on WorkflowInterruptEvent { interruptId question } ... on WorkflowInterruptResolvedEvent { interruptId }
  ... on WorkflowBudgetExceededEvent { reason snapshot }
  ... on WorkflowBudgetUpdateEvent { inputTokens outputTokens totalTokens llmCalls toolCalls snapshot }
  ... on WorkflowNodeRetryEvent { nodeId attempt maxRetries error }
  ... on WorkflowDoneEvent { outputs runId } ... on WorkflowErrorEvent { error runId }
  ... on WorkflowStoppedEvent { runId } } }"""

RUNNING = """{ runningTasks { id kind label parentId startedAt hasInterrupt cancelled done
  inputTokens outputTokens totalTokens llmCalls toolCalls budgetExceeded budgetReason } }"""

# Every chat event the producers emit, with the payload shapes that are easy
# to render differently: floats, non-ASCII, nested objects, explicit nulls,
# extra keys that fold into a JSON-text field, and fallbacks between keys.
CHAT_EVENTS: list[tuple[str, dict[str, Any]]] = [
    ("token", {"text": "Hello, wörld 👋", "source": "main"}),
    ("token", {"text": "no source"}),
    ("thinking_token", {"text": "hmm"}),
    ("step", {"node": "tools", "source": "main", "subagent": None,
              "data": {"tool": "run_cell", "args": {"code": "print('é')", "n": 1.0, "big": 1e-05}}}),
    ("step", {"node": "model", "source": "subagent", "subagent": "researcher:0", "data": "already text"}),
    ("browser_step", {"url": "https://example.com/ä", "phase": "done", "source": "main"}),
    ("worker_start", {"idx": 2, "role": "researcher", "task": "look"}),
    ("worker_step", {"idx": "3", "role": "coder", "node": "tools", "data": [1, 2.5, None]}),
    ("worker_token", {"idx": 1.9, "text": "w"}),
    ("worker_done", {"idx": 2, "role": "researcher", "task": "look", "result": "found"}),
    ("artifact", {"id": "a1", "title": "Notes", "action": "created", "kind": None, "preview": "# Notes"}),
    ("artifact", {"id": "a2", "title": "Chart", "action": "updated", "kind": "image"}),
    ("todos_updated", {"todos": [{"text": "plan", "status": "done"}, {"text": "do"}, {"no": "text"}, "junk"],
                       "source": "main"}),
    ("queued_message", {"message_id": "q1", "text": "also this", "position": 1}),
    ("queued_withdrawn", {"message_id": "q1"}),
    ("queued_consumed", {"message_ids": ["q2", 3]}),
    ("approval_request", {"tool": "rm", "reason": "deletes", "args": {"path": "/tmp/x"}}),
    ("approval_request", {"tool": "gate", "reason": "r", "args": "{}", "approval_id": "ap1", "deferred": True}),
    ("approval_resolved", {"tool": "rm", "approved": 1, "answer": "yes"}),
    ("workflow_event", {"parent_run_id": "p", "child_event": "node_done", "node_id": "n1", "output": {"x": 0.1}}),
    ("budget_exceeded", {"reason": "tokens", "limit": 100, "used": 101.5}),
    ("budget_update", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "llm_calls": 1,
                       "tool_calls": 0, "snapshot": {"elapsed_seconds": 1.25}}),
    ("perf_update", {"ttft_ms": 350.0, "llm_ms": None, "prefill_tps": "41.5", "eval_tps": 38.123456789,
                     "llm_calls": 1}),
    ("not_a_chat_event", {"x": 1}),
    ("done", {"message": "All done ✓", "conversation_id": "c1"}),
]

WORKFLOW_EVENTS: list[tuple[str, dict[str, Any]]] = [
    ("node_start", {"node_id": "n1", "node_type": "agent", "label": "Research"}),
    ("node_token", {"node_id": "n1", "text": "t"}),
    ("node_condition", {"node_id": "c", "verdict": True}),
    ("node_done", {"node_id": "n1", "output": {"result": "ok", "score": 0.5, "nested": {"a": [1]}}}),
    ("node_done", {"node_id": "n2", "output": None}),
    ("node_error", {"node_id": "n3", "error": "boom"}),
    ("map_start", {"node_id": "m", "total": "3"}),
    ("map_item_done", {"node_id": "m", "index": 0, "result": {"v": 1}}),
    ("approval_request", {"tool": "approve", "reason": "ok?", "args": {"k": "v"}}),
    ("approval_request", {"tool": "approve", "reason": "ok?", "args": "raw", "node_id": "ap"}),
    ("approval_resolved", {"tool": "approve", "approved": True, "answer": "yes", "node_id": ""}),
    ("interrupt", {"interrupt_id": "i", "question": "q"}),
    ("interrupt_resolved", {"interrupt_id": "i"}),
    ("budget_exceeded", {"reason": "calls", "snapshot": {"calls": 9}}),
    ("budget_update", {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3, "llm_calls": 1, "tool_calls": 0}),
    ("node_retry", {"node_id": "n1", "attempt": 1, "max_retries": 3, "error": "flaky"}),
    ("token", {"text": "not a workflow event"}),
    ("workflow_done", {"outputs": {"answer": 42}, "run_id": "wr"}),
]


# ── harness ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def edge(database, work_dir: Path, edge_binary: Path):
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db") as client:
        yield client


async def subscribe(client: httpx.AsyncClient, query: str, variables: dict[str, Any], *,
                    protocol: str = "graphql-transport-ws") -> list[dict[str, Any]]:
    """Run one subscription on the edge's WebSocket to completion."""
    import websockets

    legacy = protocol == "graphql-ws"
    async with websockets.connect(f"ws://127.0.0.1:{client.base_url.port}/graphql", subprotocols=[protocol]) as ws:
        await ws.send(json.dumps({"type": "connection_init", "payload": {}}))
        assert json.loads(await ws.recv())["type"] == "connection_ack"
        await ws.send(json.dumps({"id": "1", "type": "start" if legacy else "subscribe",
                                  "payload": {"query": query, "variables": variables}}))
        out = []
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if msg["type"] in ("next", "data"):
                out.append(msg["payload"])
            elif msg["type"] == "complete":
                return out
            elif msg["type"] == "error":
                out.append({"errors": msg["payload"]})
                return out


def _context(session) -> dict[str, Any]:
    from server.graphql.extensions import SESSION_LOCK_KEY

    return {"session": session, SESSION_LOCK_KEY: asyncio.Lock(), "caller": "human"}


async def _python_subscribe(query: str, variables: dict[str, Any]) -> list[dict[str, Any]]:
    from db import async_session
    from server.graphql.schema import schema

    out = []
    async with async_session() as s:
        stream = await schema.subscribe(query, variable_values=variables, context_value=_context(s))
        async for result in stream:
            item: dict[str, Any] = {"data": result.data}
            if result.errors:
                item["errors"] = [{"message": e.message} for e in result.errors]
            out.append(item)
    return out


def _register(task_id: str, kind: str = "chat", label: str = "run", parent_id: str | None = "c1"):
    from core.state import TaskState, _tasks

    state = TaskState(kind=kind, label=label, parent_id=parent_id)  # type: ignore[arg-type]
    _tasks[task_id] = state
    return state


def _finish(state) -> None:
    from core.state import _notify

    state.done = True
    _notify(state)


# ── streams ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("query", [CHAT, AUTOMATION, BOARD, WORKFLOW], ids=["chat", "automation", "board", "workflow"])
async def test_a_finished_run_replays_identically(database, edge_binary, query):
    """Every event type through every coercer — including the events each
    union doesn't render, which both sides must skip."""

    async def python() -> tuple[list, list]:
        from core.state import _tasks, emit_event

        state = _register("run-1")
        for name, data in CHAT_EVENTS + WORKFLOW_EVENTS:
            emit_event(state, name, **data)
        _finish(state)
        try:
            return list(state.events), await _python_subscribe(query, {"id": "run-1"})
        finally:
            _tasks.clear()

    events, expected = await recorded(python)
    assert len(expected) > 3
    case = {"id": "run-1", "kind": "chat", "events": events, "query": query, "variables": {"id": "run-1"}}
    out = subprocess.run([str(edge_binary), "--replay-events"], input=json.dumps(case) + "\n",
                         capture_output=True, text=True, check=True).stdout
    got = [{"data": r.get("data"), **({"errors": [{"message": e["message"]} for e in r["errors"]]} if r.get("errors") else {})}
           for r in json.loads(out)]
    assert got == expected


# ── fallbacks: runs that aren't live ─────────────────────────────────────────


@pytest.fixture
async def finished_rows(database):
    from datetime import datetime, timezone

    from db import async_session
    from db.models import AutomationRun, BoardTask, Conversation, Message, Workflow, WorkflowRun

    at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    async with async_session() as s:
        s.add(Conversation(id="c1", model="m", created_at=at))
        s.add_all([
            Message(id="m-done", conversation_id="c1", role="assistant", content="answer", status="done", created_at=at),
            Message(id="m-err", conversation_id="c1", role="assistant", content="", status="error", created_at=at),
            Message(id="m-running", conversation_id="c1", role="assistant", content="", status="running", created_at=at),
            AutomationRun(id="ar-done", automation_id="a", status="done", triggered_by="manual", output="out", started_at=at),
            AutomationRun(id="ar-err", automation_id="a", status="error", triggered_by="manual", error="", started_at=at),
            Workflow(id="w", name="w", created_at=at, updated_at=at),
            WorkflowRun(id="wr-done", workflow_id="w", status="done", outputs='{"k": [1, 2.0]}', started_at=at),
            WorkflowRun(id="wr-err", workflow_id="w", status="error", error="bad", started_at=at),
            BoardTask(id="b1", title="t", status="done", summary="did it", job_id="job-done", created_at=at, updated_at=at),
            BoardTask(id="b2", title="t", status="blocked", blocked_reason=None, job_id="job-blocked", created_at=at,
                      updated_at=at),
        ])
        await s.commit()


@pytest.mark.parametrize("query, run_id", [
    (CHAT, "m-done"), (CHAT, "m-err"), (CHAT, "missing"),
    (AUTOMATION, "ar-done"), (AUTOMATION, "ar-err"), (AUTOMATION, "missing"),
    (BOARD, "job-done"), (BOARD, "job-blocked"), (BOARD, "missing"),
    (WORKFLOW, "wr-done"), (WORKFLOW, "wr-err"), (WORKFLOW, "missing"),
])
async def test_fallbacks_for_runs_not_live(edge, finished_rows, query, run_id):
    expected = await recorded(lambda: _python_subscribe(query, {"id": run_id}))
    for protocol in ("graphql-transport-ws", "graphql-ws"):
        assert await subscribe(edge, query, {"id": run_id}, protocol=protocol) == expected, protocol


async def test_an_in_progress_row_that_never_registers_falls_back(edge, finished_rows):
    """A row still marked in progress may belong to a run being registered;
    the edge waits briefly for it, then answers from the row."""
    expected = await recorded(lambda: _python_subscribe(CHAT, {"id": "m-running"}))
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await subscribe(edge, CHAT, {"id": "m-running"}) == expected
    assert loop.time() - started >= 1.9  # the grace period was spent
