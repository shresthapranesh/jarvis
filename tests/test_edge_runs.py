"""The run mirror: Python's live runs, reported over the worker link
(`core/edge_link.py`) and served by the Rust edge (`edge/src/runs.rs`).

The edge now answers every subscription and `runningTasks`, and the stop
mutations, for runs that execute in this process. So the test is the same as
the query parity tests, but over time: register runs and emit events here
through the real `emit_event`, link a real `EdgeLink` to a real edge, then
subscribe through both Python's own schema and the edge's WebSocket and
require the same stream.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from edge_support import _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture

CHAT = """subscription($id: String!) { taskEvents(taskId: $id) { __typename
  ... on TokenEvent { text source } ... on ThinkingTokenEvent { text source }
  ... on StepEvent { node source subagent data } ... on BrowserStepEvent { url phase source }
  ... on WorkerStartEvent { idx role task } ... on WorkerStepEvent { idx role node data }
  ... on WorkerTokenEvent { idx text } ... on WorkerDoneEvent { idx role task status result }
  ... on ArtifactEvent { artifactId title action kind preview }
  ... on TodosUpdatedEvent { todos { text status } source }
  ... on QueuedMessageEvent { messageId text position } ... on QueuedWithdrawnEvent { messageId }
  ... on QueuedConsumedEvent { messageIds } ... on InterruptEvent { interruptId question }
  ... on InterruptResolvedEvent { interruptId }
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
    ("interrupt", {"interrupt_id": "i1", "question": "Proceed?"}),
    ("interrupt_resolved", {"interrupt_id": "i1"}),
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
async def linked(database, work_dir: Path, edge_binary: Path):
    """An edge over the test database, with this process linked to it."""
    from core.edge_link import EdgeLink
    from core.state import _tasks

    async with _run_edge(edge_binary, work_dir, work_dir / "database.db") as client:
        link = EdgeLink(f"ws://127.0.0.1:{client.base_url.port}/internal/worker")
        link.start()
        await asyncio.wait_for(link.connected.wait(), 10)
        # `connected` is set once the snapshot is queued; the edge has the
        # link once it routes a linked field here instead of to the dead backend.
        await _until(lambda: _edge_owns_runs(client))
        try:
            yield Linked(client, link)
        finally:
            await link.stop()
            _tasks.clear()


class Linked:
    def __init__(self, client: httpx.AsyncClient, link: Any):
        self.client, self.link = client, link
        self.port = client.base_url.port

    async def subscribe(self, query: str, variables: dict[str, Any], *, protocol: str = "graphql-transport-ws",
                        until: Any = None) -> list[dict[str, Any]]:
        """Run one subscription on the edge's WebSocket to completion."""
        import websockets

        legacy = protocol == "graphql-ws"
        async with websockets.connect(f"ws://127.0.0.1:{self.port}/graphql", subprotocols=[protocol]) as ws:
            await ws.send(json.dumps({"type": "connection_init", "payload": {}}))
            assert json.loads(await ws.recv())["type"] == "connection_ack"
            await ws.send(json.dumps({"id": "1", "type": "start" if legacy else "subscribe",
                                      "payload": {"query": query, "variables": variables}}))
            if until is not None:
                await until()
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

    async def post(self, query: str, variables: dict[str, Any] | None = None) -> httpx.Response:
        return await self.client.post("/graphql", json={"query": query, "variables": variables or {}})


async def _edge_owns_runs(client: httpx.AsyncClient) -> bool:
    return (await client.post("/graphql", json={"query": "{ runningTasks { id } }"})).status_code == 200


async def _until(check, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.05)


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


async def _python(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    from db import async_session
    from server.graphql.schema import schema

    async with async_session() as s:
        res = await schema.execute(query, variable_values=variables, context_value=_context(s))
    out: dict[str, Any] = {"data": res.data}
    if res.errors:
        out["errors"] = [{"message": e.message} for e in res.errors]
    return out


def _messages_only(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Errors compared by message; locations and paths are each server's own."""
    return [
        {"data": i.get("data"), **({"errors": [{"message": e["message"]} for e in i["errors"]]} if i.get("errors") else {})}
        for i in items
    ]


def _register(task_id: str, kind: str = "chat", label: str = "run", parent_id: str | None = "c1", started_at=None):
    from core.state import TaskState, _tasks

    state = TaskState(kind=kind, label=label, parent_id=parent_id)  # type: ignore[arg-type]
    if started_at is not None:
        state.started_at = started_at
    _tasks[task_id] = state
    return state


def _finish(state) -> None:
    from core.state import _notify

    state.done = True
    _notify(state)


# ── streams ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("query", [CHAT, AUTOMATION, BOARD, WORKFLOW], ids=["chat", "automation", "board", "workflow"])
async def test_a_finished_run_replays_identically(linked, query):
    """Every event type through every coercer — including the events each
    union doesn't render, which both sides must skip."""
    from core.state import emit_event

    state = _register("run-1")
    for name, data in CHAT_EVENTS + WORKFLOW_EVENTS:
        emit_event(state, name, **data)
    _finish(state)

    await _until(lambda: _mirrored_done(linked, "run-1"))
    expected = await _python_subscribe(query, {"id": "run-1"})
    assert len(expected) > 3
    assert await linked.subscribe(query, {"id": "run-1"}) == expected


async def _mirrored_done(linked: Linked, task_id: str) -> bool:
    data = (await linked.post(RUNNING)).json()["data"]["runningTasks"]
    return any(t["id"] == task_id and t["done"] for t in data)


async def test_a_live_run_streams_as_it_happens(linked):
    """Produce while subscribed: events arrive as the run goes, and `done`
    ends the stream — on both WebSocket protocols."""
    from core.state import emit_event

    for protocol in ("graphql-transport-ws", "graphql-ws"):
        task_id = f"run-live-{protocol}"
        state = _register(task_id)

        async def produce():
            await asyncio.sleep(0.2)  # let the subscriber attach first
            for name, data in CHAT_EVENTS:
                emit_event(state, name, **data)
                await asyncio.sleep(0.005)
            _finish(state)

        producer = asyncio.create_task(produce())
        got = await linked.subscribe(CHAT, {"id": task_id}, protocol=protocol)
        await producer
        assert got == await _python_subscribe(CHAT, {"id": task_id})
        assert got[-1]["data"]["taskEvents"]["__typename"] == "DoneEvent"


async def test_subscription_waits_on_a_live_run(linked):
    """A subscriber attached mid-run receives later events, then completes on done."""
    from core.state import emit_event

    state = _register("run-wait")
    emit_event(state, "token", text="first")

    async def later():
        await asyncio.sleep(0.3)
        emit_event(state, "token", text="second")
        emit_event(state, "done", message="m", conversation_id="c1")
        _finish(state)

    task = asyncio.create_task(later())
    got = await linked.subscribe(CHAT, {"id": "run-wait"})
    await task
    assert [g["data"]["taskEvents"].get("text") for g in got] == ["first", "second", None]


# ── fallbacks: runs the mirror doesn't have ──────────────────────────────────


@pytest.fixture
async def finished_rows(database):
    from datetime import datetime, timezone

    from db import async_session
    from db.models import AutomationRun, BoardTask, Conversation, Message, Workflow, WorkflowRun

    now = datetime.now(timezone.utc)
    async with async_session() as s:
        s.add(Conversation(id="c1", model="m"))
        s.add_all([
            Message(id="m-done", conversation_id="c1", role="assistant", content="answer", status="done"),
            Message(id="m-err", conversation_id="c1", role="assistant", content="", status="error"),
            AutomationRun(id="ar-done", automation_id="a", status="done", triggered_by="manual", output="out"),
            AutomationRun(id="ar-err", automation_id="a", status="error", triggered_by="manual", error=""),
            Workflow(id="w", name="w"),
            WorkflowRun(id="wr-done", workflow_id="w", status="done", outputs='{"k": [1, 2.0]}'),
            WorkflowRun(id="wr-err", workflow_id="w", status="error", error="bad"),
            BoardTask(id="b1", title="t", status="done", summary="did it", job_id="job-done"),
            BoardTask(id="b2", title="t", status="blocked", blocked_reason=None, job_id="job-blocked"),
        ])
        await s.commit()
    del now


@pytest.mark.parametrize("query, run_id", [
    (CHAT, "m-done"), (CHAT, "missing"),
    (AUTOMATION, "ar-done"), (AUTOMATION, "ar-err"), (AUTOMATION, "missing"),
    (BOARD, "job-done"), (BOARD, "job-blocked"), (BOARD, "missing"),
    (WORKFLOW, "wr-done"), (WORKFLOW, "wr-err"), (WORKFLOW, "missing"),
])
async def test_fallbacks_for_runs_not_live(linked, finished_rows, query, run_id):
    assert await linked.subscribe(query, {"id": run_id}) == await _python_subscribe(query, {"id": run_id})


async def test_an_in_progress_row_waits_for_its_run(linked, finished_rows):
    """A row still marked in progress may belong to a run whose registration
    is racing the subscription; the edge waits briefly for it."""
    from core.state import emit_event

    async def register_late():
        await asyncio.sleep(0.4)
        state = _register("m-err")
        emit_event(state, "done", message="late", conversation_id="c1")
        _finish(state)

    task = asyncio.create_task(register_late())
    got = await linked.subscribe(CHAT, {"id": "m-err"})
    await task
    assert got == [{"data": {"taskEvents": {"__typename": "DoneEvent", "message": "late", "conversationId": "c1"}}}]


async def test_an_in_progress_row_that_never_registers_falls_back(linked, finished_rows):
    loop = asyncio.get_running_loop()
    started = loop.time()
    got = await linked.subscribe(CHAT, {"id": "m-err"})
    assert got == await _python_subscribe(CHAT, {"id": "m-err"})
    assert loop.time() - started >= 1.9  # the grace period was spent


# ── runningTasks and stop ────────────────────────────────────────────────────


async def test_running_tasks_mirror_python(linked):
    from core.state import InterruptRequest, emit_event

    a = _register("t-a", kind="chat", label="Chat one")
    # A tie keeps registration order, on both sides.
    b = _register("t-b", kind="workflow", label="Flow", parent_id=None, started_at=a.started_at)
    c = _register("t-c", kind="automation", label="Nightly")
    a.input_tokens, a.output_tokens, a.llm_calls, a.tool_calls = 1200, 340, 3, 2  # no notify: the sweep must find it
    b.set_interrupt(InterruptRequest(id="i", question="?"))
    emit_event(b, "interrupt", interrupt_id="i", question="?")
    c.budget_exceeded, c.budget_reason = True, "too many tokens"
    _finish(c)

    async def same() -> bool:
        return (await linked.post(RUNNING)).json() == await _python(RUNNING)

    await _until(same)


async def test_stop_mutations_reach_the_run(linked, database):
    from datetime import datetime, timezone

    from db import async_session
    from db.models import Job

    now = datetime.now(timezone.utc)
    async with async_session() as s:
        s.add_all([
            Job(id="s-pending", kind="chat", payload="{}", status="pending", run_at=now),
            Job(id="s-running", kind="workflow", payload="{}", status="running", run_at=now),
        ])
        await s.commit()

    pending = _register("s-pending")
    running = _register("s-running", kind="workflow")
    loop = asyncio.get_running_loop()
    running.resume_future = loop.create_future()
    await _until(lambda: _mirrored(linked, "s-running"))

    resp = await linked.post("mutation($id: String!) { stopRunningTask(taskId: $id) { ok taskId kind } }", {"id": "s-pending"})
    assert resp.json() == {"data": {"stopRunningTask": {"ok": True, "taskId": "s-pending", "kind": "chat"}}}
    resp = await linked.post("mutation($id: String!) { stopWorkflowRun(runId: $id) }", {"id": "s-running"})
    assert resp.json() == {"data": {"stopWorkflowRun": True}}

    # The in-process half arrives over the link…
    await _until(lambda: _is(lambda: pending.cancelled and pending._stop_event.is_set() and running.cancelled))
    # …and only stopRunningTask cancels a pending answer, as in Python.
    assert not running.resume_future.cancelled()
    # The durable half is in the jobs table.
    async with async_session() as s:
        jobs = {j.id: j for j in (await s.execute(__import__("sqlalchemy").select(Job))).scalars()}
    assert jobs["s-pending"].status == "cancelled" and jobs["s-pending"].completed_at is not None
    assert jobs["s-running"].status == "running" and jobs["s-running"].cancel_requested

    # The errors match Python's.
    _finish(pending)
    await _until(lambda: _mirrored_done(linked, "s-pending"))
    for query, var in [
        ("mutation($id: String!) { stopRunningTask(taskId: $id) { ok } }", "s-pending"),
        ("mutation($id: String!) { stopTask(taskId: $id) }", "nope"),
        ("mutation($id: String!) { stopAutomationRun(runId: $id) }", "s-pending"),
        ("mutation($id: String!) { stopWorkflowRun(runId: $id) }", "nope"),
    ]:
        edge = (await linked.post(query, {"id": var})).json()
        assert [e["message"] for e in edge["errors"]] == [e["message"] for e in (await _python(query, {"id": var}))["errors"]]


async def _mirrored(linked: Linked, task_id: str) -> bool:
    return any(t["id"] == task_id for t in (await linked.post(RUNNING)).json()["data"]["runningTasks"])


async def _is(predicate) -> bool:
    return bool(predicate())


# ── the link itself ──────────────────────────────────────────────────────────


async def test_unregistered_runs_leave_the_mirror(linked):
    from core.state import _tasks

    _register("gone-soon")
    await _until(lambda: _mirrored(linked, "gone-soon"))
    _tasks.pop("gone-soon")
    await _until(lambda: _not(_mirrored(linked, "gone-soon")))


async def _not(awaitable) -> bool:
    return not await awaitable


async def test_a_dropped_link_hands_runs_back_to_python(linked):
    """With no worker linked, the mirror isn't current: linked fields and the
    subscription socket go to Python (the dead backend here)."""
    await linked.link.stop()
    await _until(lambda: _not(_edge_owns_runs(linked.client)))
    import websockets

    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(f"ws://127.0.0.1:{linked.port}/graphql", subprotocols=["graphql-transport-ws"]):
            pass


async def test_a_reconnect_from_the_same_process_keeps_subscribers(linked):
    """A subscriber attached before the link drops keeps its stream when the
    same process reconnects: the snapshot extends the run in place."""
    from core.state import emit_event

    state = _register("survivor")
    emit_event(state, "token", text="before")
    await _until(lambda: _mirrored(linked, "survivor"))

    async def bounce_then_finish():
        await asyncio.sleep(0.2)
        ws = linked.link._ws
        await ws.close()  # the link drops; EdgeLink reconnects on its own
        emit_event(state, "token", text="while down")
        await asyncio.wait_for(_reconnected(linked.link), 10)
        emit_event(state, "done", message="m", conversation_id="c1")
        _finish(state)

    task = asyncio.create_task(bounce_then_finish())
    got = await linked.subscribe(CHAT, {"id": "survivor"})
    await task
    assert [g["data"]["taskEvents"].get("text") for g in got] == ["before", "while down", None]


async def _reconnected(link) -> None:
    while link.connected.is_set():
        await asyncio.sleep(0.01)
    await link.connected.wait()


async def test_a_new_worker_process_ends_old_runs(linked, finished_rows):
    """Runs of a previous worker process can't be live: a subscriber on one
    gets the DB fallback instead of waiting forever."""
    from core.edge_link import EdgeLink

    _register("m-done")  # its Message row says done
    await _until(lambda: _mirrored(linked, "m-done"))

    async def replace_worker():
        await asyncio.sleep(0.2)
        await linked.link.stop()
        linked.link = EdgeLink(linked.link.url)  # a new instance id: a new process
        from core.state import _tasks

        _tasks.clear()
        linked.link.start()

    task = asyncio.create_task(replace_worker())
    got = await linked.subscribe(CHAT, {"id": "m-done"})
    await task
    assert got == [{"data": {"taskEvents": {"__typename": "DoneEvent", "message": "answer", "conversationId": "c1"}}}]
