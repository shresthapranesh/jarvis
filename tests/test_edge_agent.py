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

GOOGLE = "google_genai:gemma-4-31b-it"


async def _edge_job(session, job_id: str, *, status: str, kind: str = "chat", **fields) -> None:
    """A job as an earlier edge wrote it, `runtime` and all."""
    from db.models import EDGE_RUNTIME, Job

    session.add(Job(id=job_id, kind=kind, payload="{}", status=status, runtime=EDGE_RUNTIME, **fields))


# ── the edge's side ──────────────────────────────────────────────────────────

START = """mutation($input: StartTaskInput!) {
  startTask(input: $input) { taskId conversationId queued } }"""
EDGE_ON = {"JARVIS_RUN_JOBS": "1"}


REPO = Path(__file__).resolve().parent.parent


def _edge_env(work_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    # The checkout, for the system prompt; HOME and it hold no mcp.json, so
    # only an MCP server a test configures counts.
    return {"HOME": str(work_dir), "JARVIS_APP_DIR": str(REPO), **(extra or {})}


async def _job(job_id: str):
    from db import async_session
    from db.models import Job

    async with async_session() as s:
        return await s.get(Job, job_id)


async def _settled(job_id: str, timeout: float = 10.0):
    """The job once nothing holds it: pending, unlocked."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = await _job(job_id)
        if job is not None and job.status == "pending" and job.locked_by is None:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} never settled: {await _job(job_id)}")


async def _finished(job_id: str, timeout: float = 15.0):
    """The job once it ran to its end."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = await _job(job_id)
        if job is not None and job.status in ("done", "error", "cancelled"):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {await _job(job_id)}")


async def _start(client, **input) -> str:
    resp = await client.post("/graphql", json={"query": START, "variables": {"input": input}})
    body = resp.json()
    assert "errors" not in body, body
    return body["data"]["startTask"]["taskId"]


async def test_the_edge_takes_every_turn(database, work_dir: Path, edge_binary: Path):
    """Here one turn's model is unreachable, and the other's — Bedrock with
    credentials only boto3 read — can't be called: both fail, and say why."""
    from db import async_session
    from db.models import ConfigSetting, Message

    dead = {"OLLAMA_HOST": f"http://127.0.0.1:{_free_port()}",
            # No keys or profile: a web identity token, which only boto3 reads.
            "AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": "", "AWS_PROFILE": "", "AWS_DEFAULT_PROFILE": "",
            "AWS_WEB_IDENTITY_TOKEN_FILE": str(work_dir / "token")}
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir, {**EDGE_ON, **dead})) as client:
        served = await _start(client, query="hello", model="ollama:llama3.3")
        bedrock = await _start(client, query="hello", model="bedrock:us.anthropic.claude-sonnet-4-6")

        for task_id in (served, bedrock):
            job = await _finished(task_id)
            assert (job.attempts, job.status) == (1, "done")
            async with async_session() as s:
                message = await s.get(Message, task_id)
                assert message is not None and message.status == "error"
                assert message.content.startswith("The run failed before completing:")
        async with async_session() as s:
            message = await s.get(Message, bedrock)
            assert message is not None and "unsupported AWS credentials (web identity credentials)" in message.content

        # A configured MCP server is the edge's too.
        async with async_session() as s:
            s.add(ConfigSetting(key="mcp.servers", value=json.dumps({"fs": {"command": "x"}})))
            await s.commit()
        with_mcp = await _start(client, query="hello", model="ollama:llama3.3")
        job = await _finished(with_mcp)
        assert job.status == "done"


async def test_an_edge_start_recovers_the_jobs_the_last_one_left(database, work_dir: Path, edge_binary: Path):
    from db import async_session
    from db.models import Conversation, Job, Message

    async def seed(job_id: str) -> None:
        async with async_session() as s:
            s.add(Conversation(id=f"c-{job_id}", title="t", model=GOOGLE))
            s.add(Message(id=job_id, conversation_id=f"c-{job_id}", role="assistant", content="", status="running"))
            await _edge_job(
                s, job_id, status="running", locked_by="edge-1", thread_id=f"c-{job_id}", attempts=1,
                locked_until=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            s.add(Message(id=f"u-{job_id}", conversation_id=f"c-{job_id}", role="user", content="hi"))
            await s.commit()
        async with async_session() as s:
            job = await s.get(Job, job_id)
            assert job is not None
            job.payload = json.dumps({"query": "hi", "model": GOOGLE, "conv_id": f"c-{job_id}"})
            await s.commit()

    # The dead process's job is claimed again, and run (its model has no key
    # in a test, so it fails — here).
    await seed("left-running")
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir, EDGE_ON)):
        job = await _finished("left-running")
        assert (job.attempts, job.status) == (2, "done")

    # Queued without running them, the start still recovers it.
    await seed("not-run")
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir)):
        job = await _settled("not-run")
        assert job.attempts == 1


async def test_a_run_that_cannot_start_fails_in_the_edge(database, work_dir: Path, edge_binary: Path):
    """A chat job with no payload fails, its message saying why; an
    automation of an input type nobody knows fails its run, as Python's
    `_run_automation_inner` did."""
    from db import async_session
    from db.models import Automation, AutomationRun, Conversation, Job, Message

    async with async_session() as s:
        s.add(Conversation(id="c-bare", title="t", model=GOOGLE))
        s.add(Message(id="bare", conversation_id="c-bare", role="assistant", content="", status="running"))
        await _edge_job(s, "bare", status="pending", thread_id="c-bare")
        s.add(Automation(id="au-odd", name="odd", input_type="carrier-pigeon"))
        await _edge_job(s, "odd-run", status="pending", kind="automation")
        await s.commit()
    async with async_session() as s:
        job = await s.get(Job, "odd-run")
        assert job is not None
        job.payload = json.dumps({"automation_id": "au-odd", "triggered_by": "manual"})
        await s.commit()

    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir, EDGE_ON)):
        why = "the chat job has no query, model or conversation"
        job = await _finished("bare")
        assert (job.status, job.last_error) == ("error", why)
        job = await _finished("odd-run")
        assert job.status == "done"
    async with async_session() as s:
        message = await s.get(Message, "bare")
        assert message is not None and (message.status, message.content) == ("error", f"The run failed before completing: {why}")
        run = await s.get(AutomationRun, "odd-run")
        assert run is not None and (run.status, run.error) == ("error", "Unknown input_type: carrier-pigeon")


async def test_an_edge_start_sweeps_what_a_crash_left(database, work_dir: Path, edge_binary: Path):
    """Python's startup sweeps, run at start (`sweep.rs`): a run row no live
    job stands behind is an error, a board task with none is ready again; a
    request whose waiter died expires — a deferred action and a board task's
    question stay; an incognito conversation no live chat job belongs to is
    deleted."""
    from db import async_session
    from db.models import (
        Approval, Automation, AutomationRun, BoardTask, Conversation, Job, Message, Workflow, WorkflowRun,
    )

    async with async_session() as s:
        for cid, ephemeral in (("c", False), ("e-dead", True), ("e-live", True)):
            s.add(Conversation(id=cid, title="t", model=GOOGLE, ephemeral=ephemeral))
        for mid, cid in (("m-dead", "c"), ("m-live", "c"), ("m-e", "e-live")):
            s.add(Message(id=mid, conversation_id=cid, role="assistant", content="", status="running"))
        # Queued jobs: their rows wait for them.
        s.add(Job(id="m-live", kind="chat", payload="{}", status="pending"))
        s.add(Job(id="m-e", kind="chat", payload="{}", status="pending"))
        s.add(Automation(id="au", name="a", input_type="prompt"))
        s.add(AutomationRun(id="ar-dead", automation_id="au", status="running", triggered_by="manual"))
        s.add(Workflow(id="wf", name="w"))
        s.add(WorkflowRun(id="wr-dead", workflow_id="wf", status="running"))
        s.add(BoardTask(id="bt-dead", title="dead", status="running", job_id="gone"))
        s.add(BoardTask(id="bt-live", title="live", status="running", job_id="m-live"))
        s.add(Approval(id="gate-dead", source="tool", task_id="m-dead"))
        s.add(Approval(id="gate-kernel", source="tool"))
        s.add(Approval(id="node-dead", source="workflow", kind="input", task_id="wr-dead"))
        s.add(Approval(id="deferred", source="chat", action="delete_skill", action_payload='{"skill_id": "x"}'))
        s.add(Approval(id="board", source="board_task", kind="input", board_task_id="bt-live"))
        s.add(Approval(id="live-gate", source="tool", task_id="m-live"))
        await s.commit()

    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir)):
        # The incognito sweep is the last.
        deadline = time.monotonic() + 10
        while True:
            async with async_session() as s:
                if await s.get(Conversation, "e-dead") is None:
                    break
            assert time.monotonic() < deadline, "the incognito sweep never ran"
            await asyncio.sleep(0.05)

    async with async_session() as s:
        statuses = {mid: (await s.get(Message, mid)).status for mid in ("m-dead", "m-live", "m-e")}
        assert statuses == {"m-dead": "error", "m-live": "running", "m-e": "running"}
        for run in (await s.get(AutomationRun, "ar-dead"), await s.get(WorkflowRun, "wr-dead")):
            assert (run.status, run.error) == ("error", "interrupted by server restart") and run.finished_at
        dead, live = await s.get(BoardTask, "bt-dead"), await s.get(BoardTask, "bt-live")
        # Ready again — and maybe already dispatched anew, under a new job.
        assert dead.job_id != "gone"
        assert (live.status, live.job_id) == ("running", "m-live")
        # A queued run's gate expires too: its waiter is gone, and the run
        # asks again when it runs.
        approvals = {a: (await s.get(Approval, a)).status for a in
                     ("gate-dead", "gate-kernel", "node-dead", "deferred", "board", "live-gate")}
        assert approvals == {"gate-dead": "expired", "gate-kernel": "expired", "node-dead": "expired",
                             "deferred": "pending", "board": "pending", "live-gate": "expired"}
        assert (await s.get(Approval, "gate-dead")).result == "The run was lost when the server restarted."
        assert await s.get(Conversation, "e-live") is not None
