"""Starting and steering runs from the Rust edge (`edge/src/gql/start.rs`).

What a trigger *writes* — the conversation, the messages, the job the agent
loop claims — is diffed against Python's own trigger, recorded, exactly as
the other mutations are (`test_edge_parity.py`). Nothing claims the jobs
here; what a run *does* is `test_edge_loop.py`'s.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

from edge_support import _gid, _run_edge, edge_binary, startup_sweep, until  # noqa: F401 — edge_binary is a fixture
from python_golden import RECORD
from test_edge_parity import Twin

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


@pytest.fixture
async def twin(jarvis, work_dir: Path, tmp_path_factory, edge_binary: Path):
    import sqlite3

    await _seed()
    startup_sweep(edge_binary, work_dir, work_dir / "database.db")
    b_dir = tmp_path_factory.mktemp("twin")
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as src, \
            contextlib.closing(sqlite3.connect(b_dir / "database.db")) as dst:
        src.backup(dst)
    async with _run_edge(edge_binary, b_dir, b_dir / "database.db") as client:
        yield Twin(client, work_dir, b_dir)


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
    task = (twin.raw or first)["data"]["startTask"]["taskId"]
    edge_task = await _edge_run(twin, "chat")
    # A second message joins the run that's up (still pending, on both sides:
    # nothing claims jobs here).
    await twin.run(START, {"input": {"query": "  and also this  ", "conversationId": "c2"}})
    # The same through queueMessage, and its refusals.
    for query in ("queued", "   "):
        await twin.run(QUEUE, {"taskId": task, "query": query}, edge_variables={"taskId": edge_task, "query": query})
    await twin.run(QUEUE, {"taskId": "missing", "query": "x"})
    # Withdrawing one: once, then it's gone.
    b = _queued(twin.b_dir / "database.db", "c2")
    a = _queued(twin.a_dir / "database.db", "c2") if RECORD else b
    assert len(a) == len(b) == 2
    for _ in range(2):
        await twin.run(UNQUEUE, {"taskId": task, "messageId": a[0]},
                       edge_variables={"taskId": edge_task, "messageId": b[0]})
    await twin.run(UNQUEUE, {"taskId": "missing", "messageId": "m"})


async def test_runs_that_arent_claimed_yet_have_nothing_to_answer(twin):
    """A pending run has no interrupt to answer; an unknown one isn't there."""
    started = await twin.run(RUN_WORKFLOW, {"id": _gid("Workflow", "w1")})
    flow = (twin.raw or started)["data"]["runWorkflow"]
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


# ── a run nothing claimed ────────────────────────────────────────────────────


async def test_a_job_that_ends_unclaimed_leaves_the_running_list(database, work_dir: Path, edge_binary: Path):
    import sqlite3

    await _seed()
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db") as client:
        async def data(query: str, variables: dict | None = None) -> dict:
            body = (await client.post("/graphql", json={"query": query, "variables": variables or {}})).json()
            assert not body.get("errors"), body
            return body["data"]

        run_id = (await data(TRIGGER, {"id": _gid("Automation", "a1")}))["triggerAutomation"]
        assert [r["id"] for r in (await data(RUNNING))["runningTasks"]] == [run_id]
        with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as conn:
            conn.execute("UPDATE jobs SET status = 'error' WHERE id = ?", (run_id,))
            conn.commit()

        async def gone() -> bool:
            return (await data(RUNNING))["runningTasks"] == []

        await until(gone, timeout=12)
