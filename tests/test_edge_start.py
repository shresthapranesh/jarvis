"""Starting and steering runs from the Rust edge (`edge/src/gql/start.rs`).

Two halves, because a trigger is two things. What it *writes* — the
conversation, the messages, the documents, the job a worker will claim — is
diffed against Python's own trigger on a twin database, exactly as the other
mutations are (`test_edge_parity.py`). What it *starts* is driven end to end:
a real worker in this process, linked to a real edge, claims the job the edge
enqueued and runs it, and the stream is read back through the edge.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from edge_support import _gid, _run_edge, edge_binary, fake_worker  # noqa: F401 — edge_binary is a fixture
from test_edge_parity import Twin, _files
from test_edge_runs import CHAT, Linked, _until  # noqa: F401 — used below

START = """mutation($input: StartTaskInput!) {
  startTask(input: $input) { taskId conversationId queued queuedMessageId } }"""
QUEUE = """mutation($taskId: String!, $query: String!) {
  queueMessage(taskId: $taskId, query: $query) { messageId position } }"""
UNQUEUE = "mutation($taskId: String!, $messageId: String!) { unqueueMessage(taskId: $taskId, messageId: $messageId) }"
RUN_WORKFLOW = "mutation($id: ID!, $inputs: JSON) { runWorkflow(id: $id, inputs: $inputs) }"
RESUME_WORKFLOW = "mutation($runId: String!, $answer: String!) { resumeWorkflowRun(runId: $runId, answer: $answer) }"
APPROVE = """mutation($runId: String!, $approved: Boolean!, $answer: String) {
  resolveWorkflowApproval(runId: $runId, approved: $approved, answer: $answer) }"""
TRIGGER = "mutation($id: ID!) { triggerAutomation(id: $id) }"
RUNNING = "{ runningTasks { id kind label parentId startedAt cancelled done } }"


async def _seed() -> None:
    from db import async_session
    from db.models import Automation, ConfigSetting, Conversation, Project, Workflow

    async with async_session() as s:
        s.add_all([
            Conversation(id="c1", title="Existing", model="google_genai:gemma-4-31b-it"),
            Conversation(id="c2", title="Busy", model="google_genai:gemma-4-31b-it"),
            Project(id="p1", name="Proj"),
            Workflow(id="w1", name="Flow", definition='{"nodes": [], "edges": []}'),
            Automation(id="a1", name="Nightly", input_type="prompt", prompt_text="p"),
            # A runtime-added model, and an operator default that names it.
            ConfigSetting(key="models.custom",
                          value=json.dumps([{"id": "ollama:custom", "label": "Custom"}, {"no": "id"}, "junk"])),
            ConfigSetting(key="default.model", value="ollama:custom"),
        ])
        await s.commit()


def _stage(directories: tuple[Path, ...], upload_id: str, filename: str, mime: str, data: bytes) -> None:
    """The same staged upload on both sides of the twin."""
    for d in directories:
        (d / "staging").mkdir(exist_ok=True)
        (d / "staging" / upload_id).write_bytes(data)
        (d / "staging" / f"{upload_id}.meta.json").write_text(
            json.dumps({"filename": filename, "mime_type": mime, "size": len(data)})
        )


@pytest.fixture
async def twin(jarvis, work_dir: Path, tmp_path_factory, edge_binary: Path):
    import sqlite3

    await _seed()
    b_dir = tmp_path_factory.mktemp("twin")
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as src, \
            contextlib.closing(sqlite3.connect(b_dir / "database.db")) as dst:
        src.backup(dst)
    async with _run_edge(edge_binary, b_dir, b_dir / "database.db") as client, fake_worker(client):
        yield Twin(client, work_dir, b_dir)


def _documents(t: Twin) -> None:
    """Attachments are written under fresh uuids; their bytes must match."""
    a, b = _files(t.a_dir / "documents"), _files(t.b_dir / "documents")
    assert sorted(b.values()) == sorted(a.values())
    assert sorted(Path(n).suffix for n in b) == sorted(Path(n).suffix for n in a)


async def test_start_task_writes_what_python_writes(twin):
    # A new conversation; the request's model, a custom one, a removed one
    # (→ the operator's default), and none at all (→ the default).
    for model in ("anthropic:claude-opus-4-7", "ollama:custom", "gone:model", None):
        await twin.run(START, {"input": {"query": f"Hello wörld — {model} " + "x" * 80, "model": model}})
    # An existing conversation takes the run's model; an unknown id is created.
    await twin.run(START, {"input": {"query": "again", "conversationId": "c1", "model": "ollama:llama3.3"}})
    await twin.run(START, {"input": {"query": "fresh", "conversationId": "client-made-id"}})
    # Projects, incognito, and incognito overriding the project.
    await twin.run(START, {"input": {"query": "in a project", "projectId": "p1"}})
    await twin.run(START, {"input": {"query": "secret", "ephemeral": True}})
    await twin.run(START, {"input": {"query": "secret project", "projectId": "p1", "ephemeral": True}})
    await twin.run(START, {"input": {"query": "nope", "projectId": "missing"}})


async def test_start_task_with_attachments(twin):
    dirs = (twin.a_dir, twin.b_dir)
    _stage(dirs, "u-csv", "sales.csv", "text/csv", b"region,amount\nnorth,1\n")
    _stage(dirs, "u-png", "chart.png", "image/png", b"\x89PNG\r\n\x1a\nfake")
    _stage(dirs, "u-noext", ".env", "application/octet-stream", b"K=V")
    uploads = [{"uploadId": u} for u in ("u-csv", "u-png", "u-noext")]
    await twin.run(START, {"input": {"query": "summarize", "attachmentUploads": uploads}})
    _documents(twin)
    for d in dirs:  # consumed
        assert not list((d / "staging").iterdir())
    await twin.run(START, {"input": {"query": "summarize", "attachmentUploads": [{"uploadId": "expired"}]}})
    # An empty list is no attachments at all.
    await twin.run(START, {"input": {"query": "plain", "attachmentUploads": []}})


def _queued(db: Path, conversation_id: str) -> list[str]:
    import sqlite3

    with contextlib.closing(sqlite3.connect(db)) as conn:
        return [r[0] for r in conn.execute(
            "SELECT id FROM messages WHERE conversation_id = ? AND status = 'queued' ORDER BY rowid", (conversation_id,)
        )]


async def _edge_run(t: Twin, kind: str) -> str:
    """The edge's id for the run of `kind` its last trigger started."""
    running = (await t.edge.post("/graphql", json={"query": RUNNING})).json()["data"]["runningTasks"]
    return next(r["id"] for r in running if r["kind"] == kind)


async def test_a_busy_conversation_queues_instead(twin):
    first = await twin.run(START, {"input": {"query": "first", "conversationId": "c2"}})
    task = first["data"]["startTask"]["taskId"]
    edge_task = await _edge_run(twin, "chat")
    # A second message joins the run that's up (still pending, on both sides:
    # nothing claims jobs here); with attachments it can't.
    await twin.run(START, {"input": {"query": "  and also this  ", "conversationId": "c2"}})
    _stage((twin.a_dir, twin.b_dir), "u-doc", "notes.txt", "text/plain", b"hi")
    await twin.run(START, {"input": {"query": "with a file", "conversationId": "c2",
                                     "attachmentUploads": [{"uploadId": "u-doc"}]}})
    # The same through queueMessage, and its refusals.
    for query in ("queued", "   "):
        await twin.run(QUEUE, {"taskId": task, "query": query}, edge_variables={"taskId": edge_task, "query": query})
    await twin.run(QUEUE, {"taskId": "missing", "query": "x"})
    # Withdrawing one: once, then it's gone.
    a, b = _queued(twin.a_dir / "database.db", "c2"), _queued(twin.b_dir / "database.db", "c2")
    assert len(a) == len(b) == 2
    for _ in range(2):
        await twin.run(UNQUEUE, {"taskId": task, "messageId": a[0]},
                       edge_variables={"taskId": edge_task, "messageId": b[0]})
    await twin.run(UNQUEUE, {"taskId": "missing", "messageId": "m"})


async def test_runs_that_arent_claimed_yet_have_nothing_to_answer(twin):
    """A pending run has no interrupt to answer; an unknown one isn't there."""
    flow = (await twin.run(RUN_WORKFLOW, {"id": _gid("Workflow", "w1")}))["data"]["runWorkflow"]
    edge_flow = await _edge_run(twin, "workflow")
    for query, variables, ids in [
        (RESUME_WORKFLOW, {"answer": "a"}, {"runId": (flow, edge_flow)}),
        (RESUME_WORKFLOW, {"runId": "missing", "answer": "a"}, {}),
        (APPROVE, {"approved": True}, {"runId": (flow, edge_flow)}),
        (APPROVE, {"runId": "missing", "approved": False, "answer": "no"}, {}),
    ]:
        await twin.run(
            query, {**variables, **{k: v[0] for k, v in ids.items()}},
            edge_variables={**variables, **{k: v[1] for k, v in ids.items()}},
        )


async def test_run_workflow_and_trigger_automation(twin):
    inputs = {"topic": "naïve", "n": 3, "ratio": 0.1, "big": 1e-05, "nested": {"list": [1.0, None, True]}}
    await twin.run(RUN_WORKFLOW, {"id": _gid("Workflow", "w1"), "inputs": inputs})
    await twin.run(RUN_WORKFLOW, {"id": _gid("Workflow", "w1")})
    await twin.run(RUN_WORKFLOW, {"id": _gid("Workflow", "w1"), "inputs": [1, 2]})  # not an object → {}
    await twin.run(RUN_WORKFLOW, {"id": _gid("Workflow", "missing")})
    await twin.run(TRIGGER, {"id": _gid("Automation", "a1")})
    await twin.run(TRIGGER, {"id": _gid("Automation", "missing")})


# ── end to end: the edge starts it, a worker here runs it ────────────────────


class Scripted:
    """Stands in for the three run loops. The queue handlers around them —
    claiming, creating the run's state from the job, adopting queued
    messages — are the real ones."""

    def __init__(self) -> None:
        self.entered: list[tuple[str, Any]] = []
        self.release = asyncio.Event()

    async def _hold(self, state) -> str:
        while not self.release.is_set() and not state.cancelled:
            await asyncio.sleep(0.02)
        return "stopped" if state.cancelled else "done"

    async def chat(self, task_id, query, model, conv_id, attachments=None, invocation_context=None, handoff=None):
        from core.run_scaffold import finish_task_state
        from core.state import _tasks, emit_event

        state = _tasks[task_id]
        self.entered.append((task_id, {"cancelled": state.cancelled, "queued": [m.id for m in state.pending_input],
                                       "started_at": state.started_at}))
        emit_event(state, "token", text=f"echo: {query}", source="main")
        status = await self._hold(state)
        emit_event(state, status, message="", conversation_id=conv_id)
        finish_task_state(task_id, state, status)

    async def automation(self, auto, state, run_id, invocation_context=None):
        from core.run_scaffold import finish_task_state
        from core.state import emit_event

        self.entered.append((run_id, {"cancelled": state.cancelled}))
        status = await self._hold(state)
        emit_event(state, "stopped" if status == "stopped" else "done", output="", run_id=run_id)
        finish_task_state(run_id, state, status)

    async def workflow(self, wf, state, run_id, inputs):
        from core.run_scaffold import finish_task_state
        from core.state import emit_event

        self.entered.append((run_id, {"cancelled": state.cancelled, "inputs": inputs}))
        status = await self._hold(state)
        emit_event(state, "workflow_stopped" if status == "stopped" else "workflow_done", outputs={}, run_id=run_id)
        finish_task_state(run_id, state, status)


class Worked(Linked):
    def __init__(self, client: httpx.AsyncClient, link: Any, script: Scripted):
        super().__init__(client, link)
        self.script = script
        self._worker: asyncio.Task | None = None

    async def start_worker(self) -> None:
        """A worker that polls once a minute — anything sooner is the wake."""
        from core.queue import Worker
        from core.state import get_queue
        from server.automation_runtime import automation_job_handler
        from server.chat_runtime import chat_job_handler
        from server.workflow_runtime import workflow_job_handler

        handlers = {"chat": chat_job_handler, "automation": automation_job_handler, "workflow": workflow_job_handler}

        async def handle(job):
            await handlers[job.kind](job)

        worker = Worker(get_queue(), list(handlers), handle, worker_id="test", poll_interval=60, max_concurrent=4)
        self._worker = asyncio.create_task(worker.run())
        await asyncio.sleep(0.3)  # its first claim finds nothing; now it waits

    async def stop_worker(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker

    async def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self.post(query, variables)
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def data(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        out = await self.gql(query, variables)
        assert not out.get("errors"), out
        return out["data"]

    async def entered(self, run_id: str) -> dict[str, Any]:
        await _until(lambda: _true(any(r == run_id for r, _ in self.script.entered)))
        return next(seen for r, seen in self.script.entered if r == run_id)


async def _true(value: bool) -> bool:
    return value


@pytest.fixture
async def worked(jarvis, work_dir: Path, edge_binary: Path, monkeypatch):
    from core.edge_link import EdgeLink
    from test_edge_runs import _edge_owns_runs

    await _seed()
    script = Scripted()
    monkeypatch.setattr("server.chat_runtime._run_agent_task", script.chat)
    monkeypatch.setattr("server.automation_runtime._run_automation_inner", script.automation)
    monkeypatch.setattr("server.workflow_runtime._run_workflow_inner", script.workflow)
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db") as client:
        link = EdgeLink(f"ws://127.0.0.1:{client.base_url.port}/internal/worker")
        link.start()
        await _until(lambda: _edge_owns_runs(client))
        w = Worked(client, link, script)
        try:
            yield w
        finally:
            script.release.set()
            await w.stop_worker()
            await link.stop()


def _job(work_dir: Path, job_id: str) -> dict[str, Any]:
    import sqlite3

    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def _typenames(items: list[dict[str, Any]]) -> list[str]:
    return [i["data"]["taskEvents"]["__typename"] for i in items]


async def test_a_started_run_is_claimed_at_once_and_streams(worked, work_dir):
    await worked.start_worker()
    t0 = asyncio.get_running_loop().time()
    start = await worked.data(START, {"input": {"query": "hi there " + "y" * 70}})
    task_id = start["startTask"]["taskId"]
    seen = await worked.entered(task_id)
    # Woken, not polled: the worker waits a minute between polls.
    assert asyncio.get_running_loop().time() - t0 < 3

    # The run keeps the trigger's identity, and starts when the user asked.
    running = (await worked.data(RUNNING))["runningTasks"]
    assert running == [{"id": task_id, "kind": "chat", "label": "hi there " + "y" * 51,
                        "parentId": start["startTask"]["conversationId"],
                        "startedAt": running[0]["startedAt"], "cancelled": False, "done": False}]
    created = datetime.fromisoformat(_job(work_dir, task_id)["created_at"]).replace(tzinfo=timezone.utc)
    assert seen["started_at"] == created
    assert running[0]["startedAt"] == created.isoformat()

    async def release():
        worked.script.release.set()

    events = await worked.subscribe(CHAT, {"id": task_id}, until=release)
    assert _typenames(events) == ["TokenEvent", "DoneEvent"]
    assert events[0]["data"]["taskEvents"]["text"] == "echo: hi there " + "y" * 70


async def test_messages_queued_before_the_claim_reach_the_run(worked):
    start = (await worked.data(START, {"input": {"query": "first"}}))["startTask"]
    task_id = start["taskId"]
    q1 = (await worked.data(QUEUE, {"taskId": task_id, "query": " one "}))["queueMessage"]
    q2 = (await worked.data(QUEUE, {"taskId": task_id, "query": "two"}))["queueMessage"]
    assert (q1["position"], q2["position"]) == (1, 2)
    assert (await worked.data(UNQUEUE, {"taskId": task_id, "messageId": q1["messageId"]}))["unqueueMessage"] is True
    # Starting a turn on the busy conversation queues too.
    again = (await worked.data(START, {"input": {"query": "three", "conversationId": start["conversationId"]}}))
    assert again["startTask"]["taskId"] == task_id and again["startTask"]["queued"] is True

    await worked.start_worker()
    seen = await worked.entered(task_id)
    assert seen["queued"] == [q2["messageId"], again["startTask"]["queuedMessageId"]]

    async def release():
        worked.script.release.set()

    events = await worked.subscribe(CHAT, {"id": task_id}, until=release)
    assert _typenames(events) == ["QueuedMessageEvent", "QueuedMessageEvent", "QueuedWithdrawnEvent",
                                  "QueuedMessageEvent", "TokenEvent", "DoneEvent"]
    assert events[0]["data"]["taskEvents"] == {"__typename": "QueuedMessageEvent", "messageId": q1["messageId"],
                                               "text": "one", "position": 1}


async def test_a_run_stopped_before_its_claim_starts_stopped(worked, work_dir):
    chat = (await worked.data(START, {"input": {"query": "never mind"}}))["startTask"]["taskId"]
    auto = (await worked.data(TRIGGER, {"id": _gid("Automation", "a1")}))["triggerAutomation"]
    assert (await worked.data("mutation($id: String!) { stopTask(taskId: $id) }", {"id": chat}))["stopTask"] is True
    stopped = await worked.data("mutation($id: String!) { stopRunningTask(taskId: $id) { ok kind } }", {"id": auto})
    assert stopped["stopRunningTask"] == {"ok": True, "kind": "automation"}
    # Durable, and still claimable: the stop rides on the job.
    for job_id in (chat, auto):
        job = _job(work_dir, job_id)
        assert (job["status"], job["cancel_requested"]) == ("pending", 1)
    assert all(r["cancelled"] for r in (await worked.data(RUNNING))["runningTasks"])

    await worked.start_worker()
    assert (await worked.entered(chat))["cancelled"] is True
    assert (await worked.entered(auto))["cancelled"] is True
    events = await worked.subscribe(CHAT, {"id": chat})
    assert _typenames(events) == ["TokenEvent", "StoppedEvent"]


async def test_a_claimed_run_is_steered_through_the_worker(worked):
    from core.state import _tasks

    await worked.start_worker()
    task_id = (await worked.data(START, {"input": {"query": "long job"}}))["startTask"]["taskId"]
    await worked.entered(task_id)
    await _until(lambda: _true(_claimed_on_edge(worked, task_id)))
    state = _tasks[task_id]

    # Queueing goes to the run's own list, by the worker's own function.
    q = (await worked.data(QUEUE, {"taskId": task_id, "query": "also"}))["queueMessage"]
    assert q["position"] == 1 and [m.id for m in state.pending_input] == [q["messageId"]]
    assert (await worked.data(UNQUEUE, {"taskId": task_id, "messageId": q["messageId"]}))["unqueueMessage"] is True
    assert (await worked.data(UNQUEUE, {"taskId": task_id, "messageId": q["messageId"]}))["unqueueMessage"] is False

    # A workflow's interrupt is answered in the worker, with the worker's errors.
    flow = (await worked.data(RUN_WORKFLOW, {"id": _gid("Workflow", "w1"), "inputs": {"k": 1}}))["runWorkflow"]
    assert (await worked.entered(flow))["inputs"] == {"k": 1}
    await _until(lambda: _true(_claimed_on_edge(worked, flow)))
    wf_state = _tasks[flow]
    refused = await worked.gql(APPROVE, {"runId": flow, "approved": True})
    assert [e["message"] for e in refused["errors"]] == ["no pending approval for this run"]
    wf_state.resume_future = asyncio.get_running_loop().create_future()
    assert (await worked.data(APPROVE, {"runId": flow, "approved": False}))["resolveWorkflowApproval"] is True
    assert wf_state.resume_future.result() == {"approved": False, "answer": "denied"}
    wf_state.resume_future = asyncio.get_running_loop().create_future()
    assert (await worked.data(RESUME_WORKFLOW, {"runId": flow, "answer": "go"}))["resumeWorkflowRun"] is True
    assert wf_state.resume_future.result() == "go"

    async def release():
        worked.script.release.set()

    events = await worked.subscribe(CHAT, {"id": task_id}, until=release)
    assert _typenames(events) == ["TokenEvent", "QueuedMessageEvent", "QueuedWithdrawnEvent", "DoneEvent"]


def _claimed_on_edge(worked: Worked, run_id: str) -> bool:
    from core.state import _tasks

    # Registered here and reported: the link has no unsent register for it.
    return run_id in _tasks and worked.link._cursor.get(run_id) is not None


async def test_a_job_that_ends_unclaimed_leaves_the_mirror(worked, work_dir):
    import sqlite3

    run_id = (await worked.data(TRIGGER, {"id": _gid("Automation", "a1")}))["triggerAutomation"]
    assert [r["id"] for r in (await worked.data(RUNNING))["runningTasks"]] == [run_id]
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as conn:
        conn.execute("UPDATE jobs SET status = 'error' WHERE id = ?", (run_id,))
        conn.commit()

    async def gone() -> bool:
        return (await worked.data(RUNNING))["runningTasks"] == []

    await _until(gone, timeout=12)


async def test_the_link_announces_what_it_adopts(jarvis):
    """`adopt_queued` from the edge: messages queued while the claim was in
    flight are adopted, and the ones the edge couldn't announce are."""
    from core.edge_link import EdgeLink
    from core.state import TaskState, _tasks
    from db import async_session
    from db.ops import add_message

    await _seed()
    async with async_session() as s:
        old = await add_message(s, "c1", "user", "left from before", status="queued")
        new = await add_message(s, "c1", "user", "just now", status="queued")
    state = TaskState(kind="chat", label="l", parent_id="c1")
    _tasks["t"] = state
    await EdgeLink("ws://unused")._adopt_queued("t", {new.id})
    assert [m.id for m in state.pending_input] == [old.id, new.id]
    assert [json.loads(e["data"]) for e in state.events] == [
        {"message_id": new.id, "text": "just now", "position": 2}
    ]
