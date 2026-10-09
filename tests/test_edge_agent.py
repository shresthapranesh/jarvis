"""The agent loop's jobs (`edge/src/agent/`): every turn is the edge's to
run, a start recovers the jobs the last process left and sweeps up what a
crash left behind, and a run that can't start fails with the reason.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from edge_support import _free_port, _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture
from seed import insert, row

GOOGLE = "google_genai:gemma-4-31b-it"


def _edge_job(db: Path, job_id: str, *, status: str, kind: str = "chat", **fields) -> None:
    """A job as an earlier edge wrote it, `runtime` and all."""
    insert(db, "jobs", id=job_id, kind=kind, status=status, runtime="edge", **{"payload": "{}", **fields})


# ── the edge's side ──────────────────────────────────────────────────────────

START = """mutation($input: StartTaskInput!) {
  startTask(input: $input) { taskId conversationId queued } }"""
EDGE_ON = {"JARVIS_RUN_JOBS": "1"}


REPO = Path(__file__).resolve().parent.parent


def _edge_env(work_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    # The checkout, for the system prompt; HOME and it hold no mcp.json, so
    # only an MCP server a test configures counts.
    return {"HOME": str(work_dir), "JARVIS_APP_DIR": str(REPO), **(extra or {})}


async def _settled(db: Path, job_id: str, timeout: float = 10.0) -> dict:
    """The job once nothing holds it: pending, unlocked."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = row(db, "jobs", job_id)
        if job is not None and job["status"] == "pending" and job["locked_by"] is None:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} never settled: {row(db, 'jobs', job_id)}")


async def _finished(db: Path, job_id: str, timeout: float = 15.0) -> dict:
    """The job once it ran to its end."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = row(db, "jobs", job_id)
        if job is not None and job["status"] in ("done", "error", "cancelled"):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {row(db, 'jobs', job_id)}")


async def _start(client, **input) -> str:
    resp = await client.post("/graphql", json={"query": START, "variables": {"input": input}})
    body = resp.json()
    assert "errors" not in body, body
    return body["data"]["startTask"]["taskId"]


async def test_the_edge_takes_every_turn(database, work_dir: Path, edge_binary: Path):
    """Here one turn's model is unreachable, and the other's — Bedrock with
    credentials only boto3 read — can't be called: both fail, and say why."""
    dead = {"OLLAMA_HOST": f"http://127.0.0.1:{_free_port()}",
            # No keys or profile: a web identity token, which only boto3 reads.
            "AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": "", "AWS_PROFILE": "", "AWS_DEFAULT_PROFILE": "",
            "AWS_WEB_IDENTITY_TOKEN_FILE": str(work_dir / "token")}
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir, {**EDGE_ON, **dead})) as client:
        served = await _start(client, query="hello", model="ollama:llama3.3")
        bedrock = await _start(client, query="hello", model="bedrock:us.anthropic.claude-sonnet-4-6")

        for task_id in (served, bedrock):
            job = await _finished(database, task_id)
            assert (job["attempts"], job["status"]) == (1, "done")
            message = row(database, "messages", task_id)
            assert message is not None and message["status"] == "error"
            assert message["content"].startswith("The run failed before completing:")
        message = row(database, "messages", bedrock)
        assert message is not None and "unsupported AWS credentials (web identity credentials)" in message["content"]

        # A configured MCP server is the edge's too.
        insert(database, "config_settings", key="mcp.servers", value=json.dumps({"fs": {"command": "x"}}))
        with_mcp = await _start(client, query="hello", model="ollama:llama3.3")
        job = await _finished(database, with_mcp)
        assert job["status"] == "done"


async def test_an_edge_start_recovers_the_jobs_the_last_one_left(database, work_dir: Path, edge_binary: Path):
    def seed(job_id: str) -> None:
        insert(database, "conversations", id=f"c-{job_id}", title="t", model=GOOGLE)
        insert(database, "messages", id=job_id, conversation_id=f"c-{job_id}", role="assistant", content="",
               status="running")
        _edge_job(
            database, job_id, status="running", locked_by="edge-1", thread_id=f"c-{job_id}", attempts=1,
            locked_until=datetime.now(timezone.utc) + timedelta(minutes=5),
            payload=json.dumps({"query": "hi", "model": GOOGLE, "conv_id": f"c-{job_id}"}),
        )
        insert(database, "messages", id=f"u-{job_id}", conversation_id=f"c-{job_id}", role="user", content="hi")

    # The dead process's job is claimed again, and run (its model has no key
    # in a test, so it fails — here).
    seed("left-running")
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir, EDGE_ON)):
        job = await _finished(database, "left-running")
        assert (job["attempts"], job["status"]) == (2, "done")

    # Queued without running them, the start still recovers it.
    seed("not-run")
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir)):
        job = await _settled(database, "not-run")
        assert job["attempts"] == 1


async def test_a_run_that_cannot_start_fails_in_the_edge(database, work_dir: Path, edge_binary: Path):
    """A chat job with no payload fails, its message saying why; an
    automation of an input type nobody knows fails its run, as Python's
    `_run_automation_inner` did."""
    insert(database, "conversations", id="c-bare", title="t", model=GOOGLE)
    insert(database, "messages", id="bare", conversation_id="c-bare", role="assistant", content="", status="running")
    _edge_job(database, "bare", status="pending", thread_id="c-bare")
    insert(database, "automations", id="au-odd", name="odd", input_type="carrier-pigeon")
    _edge_job(database, "odd-run", status="pending", kind="automation",
              payload=json.dumps({"automation_id": "au-odd", "triggered_by": "manual"}))

    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir, EDGE_ON)):
        why = "the chat job has no query, model or conversation"
        job = await _finished(database, "bare")
        assert (job["status"], job["last_error"]) == ("error", why)
        job = await _finished(database, "odd-run")
        assert job["status"] == "done"
    message = row(database, "messages", "bare")
    assert message is not None
    assert (message["status"], message["content"]) == ("error", f"The run failed before completing: {why}")
    run = row(database, "automation_runs", "odd-run")
    assert run is not None and (run["status"], run["error"]) == ("error", "Unknown input_type: carrier-pigeon")


async def test_an_edge_start_sweeps_what_a_crash_left(database, work_dir: Path, edge_binary: Path):
    """Python's startup sweeps, run at start (`sweep.rs`): a run row no live
    job stands behind is an error, a board task with none is ready again; a
    request whose waiter died expires — a deferred action and a board task's
    question stay; an incognito conversation no live chat job belongs to is
    deleted."""
    db = database
    for cid, ephemeral in (("c", False), ("e-dead", True), ("e-live", True)):
        insert(db, "conversations", id=cid, title="t", model=GOOGLE, ephemeral=ephemeral)
    for mid, cid in (("m-dead", "c"), ("m-live", "c"), ("m-e", "e-live")):
        insert(db, "messages", id=mid, conversation_id=cid, role="assistant", content="", status="running")
    # Queued jobs: their rows wait for them.
    insert(db, "jobs", id="m-live", kind="chat", payload="{}", status="pending")
    insert(db, "jobs", id="m-e", kind="chat", payload="{}", status="pending")
    insert(db, "automations", id="au", name="a", input_type="prompt")
    insert(db, "automation_runs", id="ar-dead", automation_id="au", status="running", triggered_by="manual")
    insert(db, "workflows", id="wf", name="w")
    insert(db, "workflow_runs", id="wr-dead", workflow_id="wf", status="running")
    insert(db, "board_tasks", id="bt-dead", title="dead", status="running", job_id="gone")
    insert(db, "board_tasks", id="bt-live", title="live", status="running", job_id="m-live")
    insert(db, "approvals", id="gate-dead", source="tool", task_id="m-dead")
    insert(db, "approvals", id="gate-kernel", source="tool")
    insert(db, "approvals", id="node-dead", source="workflow", kind="input", task_id="wr-dead")
    insert(db, "approvals", id="deferred", source="chat", action="delete_skill", action_payload='{"skill_id": "x"}')
    insert(db, "approvals", id="board", source="board_task", kind="input", board_task_id="bt-live")
    insert(db, "approvals", id="live-gate", source="tool", task_id="m-live")

    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir)):
        # The incognito sweep is the last.
        deadline = time.monotonic() + 10
        while row(db, "conversations", "e-dead") is not None:
            assert time.monotonic() < deadline, "the incognito sweep never ran"
            await asyncio.sleep(0.05)

    statuses = {mid: row(db, "messages", mid)["status"] for mid in ("m-dead", "m-live", "m-e")}
    assert statuses == {"m-dead": "error", "m-live": "running", "m-e": "running"}
    for run in (row(db, "automation_runs", "ar-dead"), row(db, "workflow_runs", "wr-dead")):
        assert (run["status"], run["error"]) == ("error", "interrupted by server restart") and run["finished_at"]
    dead, live = row(db, "board_tasks", "bt-dead"), row(db, "board_tasks", "bt-live")
    # Ready again — and maybe already dispatched anew, under a new job.
    assert dead["job_id"] != "gone"
    assert (live["status"], live["job_id"]) == ("running", "m-live")
    # A queued run's gate expires too: its waiter is gone, and the run asks
    # again when it runs.
    approvals = {a: row(db, "approvals", a)["status"] for a in
                 ("gate-dead", "gate-kernel", "node-dead", "deferred", "board", "live-gate")}
    assert approvals == {"gate-dead": "expired", "gate-kernel": "expired", "node-dead": "expired",
                         "deferred": "pending", "board": "pending", "live-gate": "expired"}
    assert row(db, "approvals", "gate-dead")["result"] == "The run was lost when the server restarted."
    assert row(db, "conversations", "e-live") is not None
