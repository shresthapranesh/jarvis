"""The edge's agent loop and Python sharing one job queue (phase 2d).

A job whose `runtime` is the edge's is the edge's alone: Python never claims
it, reaps its lock, or takes its rows for a crashed run's when it starts. The
edge hands a turn over by clearing `runtime` and leaving a `handoff` in the
payload; Python then continues the turn instead of starting it again.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_harness import ModelCall, tool_call
from edge_support import _free_port, _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture
from test_agent_golden import GOOGLE, script  # noqa: F401 — the fixture


async def _edge_job(session, job_id: str, *, status: str, kind: str = "chat", **fields) -> None:
    from db.models import EDGE_RUNTIME, Job

    session.add(Job(id=job_id, kind=kind, payload="{}", status=status, runtime=EDGE_RUNTIME, **fields))


async def test_python_never_claims_or_reaps_an_edge_job(database):
    from core.queue import SqliteJobQueue
    from db import async_session
    from db.models import Job

    queue = SqliteJobQueue()
    async with async_session() as s:
        await _edge_job(s, "edge-pending", status="pending")
        await _edge_job(
            s, "edge-expired", status="running", locked_by="edge:1",
            locked_until=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        await s.commit()
    assert await queue.claim(["chat"], worker_id="w") is None
    assert await queue.reap_expired_locks() == 0
    async with async_session() as s:
        job = await s.get(Job, "edge-expired")
        assert job is not None and (job.status, job.locked_by) == ("running", "edge:1")

    # Handed over: runtime cleared, it is Python's like any other.
    async with async_session() as s:
        job = await s.get(Job, "edge-pending")
        assert job is not None
        job.runtime = None
        await s.commit()
    claimed = await queue.claim(["chat"], worker_id="w")
    assert claimed is not None and claimed.id == "edge-pending"


async def test_python_starting_leaves_the_edges_runs_alone(database):
    from core.approvals import reconcile_startup
    from db import async_session
    from db.models import Approval, Conversation, Job, Message
    from db.ops import cleanup_zombie_running_rows

    async with async_session() as s:
        s.add(Conversation(id="c", title="t", model="m"))
        s.add_all([
            Message(id="edge-run", conversation_id="c", role="assistant", content="", status="running"),
            Message(id="py-run", conversation_id="c", role="assistant", content="", status="running"),
        ])
        await _edge_job(s, "edge-run", status="running", locked_by="edge:1", thread_id="c")
        s.add(Job(id="py-run", kind="chat", payload="{}", status="running", locked_by="w"))
        s.add_all([
            Approval(id="gate-edge", source="chat", status="pending", task_id="edge-run", tool="run_cell"),
            Approval(id="gate-py", source="chat", status="pending", task_id="py-run", tool="run_cell"),
        ])
        await s.commit()
        await cleanup_zombie_running_rows(s)
    await reconcile_startup()
    async with async_session() as s:
        edge_msg, py_msg = await s.get(Message, "edge-run"), await s.get(Message, "py-run")
        edge_job, py_job = await s.get(Job, "edge-run"), await s.get(Job, "py-run")
        edge_gate, py_gate = await s.get(Approval, "gate-edge"), await s.get(Approval, "gate-py")
        assert edge_msg and py_msg and edge_job and py_job and edge_gate and py_gate
        assert (edge_msg.status, edge_job.status, edge_job.locked_by) == ("running", "running", "edge:1")
        assert (py_msg.status, py_job.status) == ("error", "pending")
        assert (edge_gate.status, py_gate.status) == ("pending", "expired")


async def test_a_handed_over_turn_continues_where_the_edge_left_it(jarvis, script):  # noqa: F811
    """The edge took the turn as far as a tool batch, ran one of its two
    calls, and handed it over: Python runs the other call, then the model."""
    from sqlalchemy import select

    from core.state import _tasks
    from core.transcript_store import apply_messages, load_thread, set_todos
    from db import async_session
    from db.models import Conversation, Job, Message, Step
    from server.chat_runtime import chat_job_handler, user_message_id

    task_id, conv_id = "turn-1", "conv-1"
    prompt = HumanMessage(content="plan it", id=user_message_id(task_id))
    reply = AIMessage(content="On it. ", id="ai-1", tool_calls=[
        tool_call("write_todos", {"todos": ["One", "Two"]}, "c1"),
        tool_call("set_todo_status", {"index": 1, "status": "done"}, "c2"),
    ])
    first = ToolMessage("Updated todo list (2 items).", tool_call_id="c1", name="write_todos", id="t-1")
    handoff = {
        "text": "On it. ",
        "step_seq": 2,
        "steps": 1,
        "usage": {"input_tokens": 500, "output_tokens": 20, "llm_calls": 1, "tool_calls": 1},
    }
    async with async_session() as s:
        s.add(Conversation(id=conv_id, title="t", model=GOOGLE))
        s.add(Message(id="u-1", conversation_id=conv_id, role="user", content="plan it"))
        s.add(Message(id=task_id, conversation_id=conv_id, role="assistant", content="", model=GOOGLE,
                      status="running"))
        s.add(Job(id=task_id, kind="chat", thread_id=conv_id, payload=json.dumps({
            "query": "plan it", "model": GOOGLE, "conv_id": conv_id, "handoff": handoff,
        })))
        await s.commit()
        await apply_messages(s, conv_id, [prompt, reply, first])
        await set_todos(s, conv_id, [{"text": "One", "status": "pending"}, {"text": "Two", "status": "pending"}])

    def respond(call: ModelCall) -> AIMessage:
        return AIMessage(content="Done.")

    script.responder = respond
    job = await jarvis.queue.claim(kinds=["chat"], worker_id="test", ttl_seconds=600)
    assert job is not None and job.id == task_id
    await chat_job_handler(job)
    state = _tasks[task_id]

    # c2 ran here — c1 didn't run again — and the model saw both results.
    [call] = script.calls
    results = [m for m in call.messages if isinstance(m, ToolMessage)]
    assert [(m.tool_call_id, m.content) for m in results] == [
        ("c1", "Updated todo list (2 items)."), ("c2", "Set todo 1 to 'done'."),
    ]
    async with async_session() as s:
        thread = await load_thread(s, conv_id)
        msg = await s.get(Message, task_id)
        steps = (await s.execute(select(Step).where(Step.message_id == task_id).order_by(Step.seq))).scalars().all()
    assert [m.id for m in thread.messages[:3]] == [prompt.id, "ai-1", "t-1"]
    assert sum(isinstance(m, HumanMessage) for m in thread.messages) == 1
    # The plan the edge wrote stands; the prompt isn't sent again.
    assert thread.todos == [{"text": "One", "status": "pending"}, {"text": "Two", "status": "done"}]
    assert not any(e["event"] == "todos_updated" and json.loads(e["data"])["todos"] == [] for e in state.events)
    # The turn's text, step rows and usage run on from the edge's.
    assert msg is not None and (msg.status, msg.content) == ("done", "On it. Done.")
    assert [(st.seq, st.node) for st in steps] == [(2, "tools"), (3, "model_request")]
    assert msg.input_tokens is not None and msg.input_tokens > 500
    assert state.llm_calls == 2 and state.tool_calls >= 1


async def test_a_handed_over_automation_run_continues_where_the_edge_left_it(jarvis, script):  # noqa: F811
    """A stateful automation's run, handed over after its first model step:
    Python runs the recorded call, then the model, and finishes the run —
    without writing the prompt into the conversation a second time."""
    from sqlalchemy import select

    from core.transcript_store import apply_messages, load_thread
    from db import async_session
    from db.models import Automation, AutomationRun, Conversation, Job, Message
    from db.ops import automation_conversation_id
    from server.automation_runtime import automation_job_handler

    run_id, auto_id = "run-1", "auto-1"
    conv_id = automation_conversation_id(auto_id)
    prompt = HumanMessage(content="check the tea", id="u-1")
    reply = AIMessage(content="Checking. ", id="ai-1", tool_calls=[tool_call("write_todos", {"todos": ["Look"]}, "c1")])
    handoff = {"text": "Checking. ", "step_seq": 0, "steps": 1,
               "usage": {"input_tokens": 300, "output_tokens": 10, "llm_calls": 1, "tool_calls": 0}}
    async with async_session() as s:
        s.add(Automation(id=auto_id, name="Tea", input_type="prompt", prompt_text="check the tea", model=GOOGLE,
                         stateful=True))
        s.add(Conversation(id=conv_id, title="Tea", model=GOOGLE, surface="automation"))
        s.add(Message(id="m-1", conversation_id=conv_id, role="user", content="check the tea"))
        s.add(AutomationRun(id=run_id, automation_id=auto_id, triggered_by="schedule", status="running"))
        s.add(Job(id=run_id, kind="automation", payload=json.dumps({
            "automation_id": auto_id, "triggered_by": "schedule", "handoff": handoff})))
        await s.commit()
        await apply_messages(s, conv_id, [prompt, reply])

    script.responder = lambda call: AIMessage(content="Still warm.")
    job = await jarvis.queue.claim(kinds=["automation"], worker_id="test", ttl_seconds=600)
    assert job is not None and job.id == run_id
    await automation_job_handler(job)

    [call] = script.calls
    assert [(m.tool_call_id, m.content) for m in call.messages if isinstance(m, ToolMessage)] == [
        ("c1", "Updated todo list (1 item)."),
    ]
    async with async_session() as s:
        thread = await load_thread(s, conv_id)
        run = await s.get(AutomationRun, run_id)
        messages = (await s.execute(
            select(Message.role, Message.content, Message.status)
            .where(Message.conversation_id == conv_id).order_by(Message.created_at)
        )).all()
    assert sum(isinstance(m, HumanMessage) for m in thread.messages) == 1
    assert run is not None and (run.status, run.output) == ("done", "Checking. Still warm.")
    assert [tuple(m) for m in messages] == [
        ("user", "check the tea", "done"), ("assistant", "Checking. Still warm.", "done"),
    ]


async def test_a_handed_over_board_run_continues_where_the_edge_left_it(jarvis, script):  # noqa: F811
    """A board run handed over after its first model step: Python runs the
    recorded complete_task, then the model, without writing the prompt into
    the task's conversation again; the task keeps the tool's summary."""
    from sqlalchemy import select

    from core.transcript_store import apply_messages, load_thread
    from db import async_session
    from db.models import BoardTask, Conversation, Job, Message
    from db.ops import board_task_conversation_id
    from server.task_board_runtime import board_task_job_handler

    run_id, task_id = "run-b", "task-b"
    conv_id = board_task_conversation_id(task_id)
    prompt = HumanMessage(content="do the task", id="u-1")
    reply = AIMessage(content="Doing. ", id="ai-1",
                      tool_calls=[tool_call("complete_task", {"summary": "Did it."}, "c1")])
    handoff = {"text": "Doing. ", "step_seq": 0, "steps": 1,
               "usage": {"input_tokens": 300, "output_tokens": 10, "llm_calls": 1, "tool_calls": 0}}
    async with async_session() as s:
        s.add(BoardTask(id=task_id, title="T", status="running", job_id=run_id, model=GOOGLE))
        s.add(Conversation(id=conv_id, title="T", model=GOOGLE, surface="task"))
        s.add(Message(id="m-1", conversation_id=conv_id, role="user", content="do the task"))
        s.add(Job(id=run_id, kind="board_task", thread_id=conv_id,
                  payload=json.dumps({"task_id": task_id, "handoff": handoff})))
        await s.commit()
        await apply_messages(s, conv_id, [prompt, reply])

    script.responder = lambda call: AIMessage(content="All set.")
    job = await jarvis.queue.claim(kinds=["board_task"], worker_id="test", ttl_seconds=600)
    assert job is not None and job.id == run_id
    await board_task_job_handler(job)

    [call] = script.calls
    assert [(m.tool_call_id, m.content) for m in call.messages if isinstance(m, ToolMessage)] == [
        ("c1", "Task marked done. Wrap up with a short final reply."),
    ]
    async with async_session() as s:
        thread = await load_thread(s, conv_id)
        task = await s.get(BoardTask, task_id)
        messages = (await s.execute(
            select(Message.role, Message.content).where(Message.conversation_id == conv_id).order_by(Message.created_at)
        )).all()
    assert sum(isinstance(m, HumanMessage) for m in thread.messages) == 1
    assert task is not None and (task.status, task.summary) == ("done", "Did it.")
    assert [tuple(m) for m in messages] == [("user", "do the task"), ("assistant", "Doing. All set.")]


async def test_a_reclaimed_turn_never_runs_a_tool_twice(jarvis, script):  # noqa: F811
    """Without a handoff — a crash, not a handover — the unanswered call is
    repaired as an orphan, as before, not run."""
    from core.transcript_store import apply_messages
    from db import async_session
    from db.models import Conversation, Job, Message
    from server.chat_runtime import chat_job_handler, user_message_id

    task_id, conv_id = "turn-2", "conv-2"
    async with async_session() as s:
        s.add(Conversation(id=conv_id, title="t", model=GOOGLE))
        s.add(Message(id=task_id, conversation_id=conv_id, role="assistant", content="", model=GOOGLE,
                      status="running"))
        s.add(Job(id=task_id, kind="chat", thread_id=conv_id, payload=json.dumps({
            "query": "plan it", "model": GOOGLE, "conv_id": conv_id,
        })))
        await s.commit()
        await apply_messages(s, conv_id, [
            HumanMessage(content="plan it", id=user_message_id(task_id)),
            AIMessage(content="", id="ai-1", tool_calls=[tool_call("write_todos", {"todos": ["X"]}, "c1")]),
        ])

    script.responder = lambda call: AIMessage(content="Done.")
    job = await jarvis.queue.claim(kinds=["chat"], worker_id="test", ttl_seconds=600)
    assert job is not None
    await chat_job_handler(job)
    [call] = script.calls
    [result] = [m for m in call.messages if isinstance(m, ToolMessage)]
    assert result.tool_call_id == "c1" and "Updated todo list" not in str(result.content)


# ── the edge's side ──────────────────────────────────────────────────────────

START = """mutation($input: StartTaskInput!) {
  startTask(input: $input) { taskId conversationId queued } }"""
EDGE_ON = {"JARVIS_AGENT_RUNTIME": "edge"}


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


async def test_the_edge_takes_the_turns_it_serves(database, work_dir: Path, edge_binary: Path):
    """A turn on a provider the edge speaks is the edge's to run (here its
    model is unreachable, so it fails — in the edge, which records it); one it
    can't make — Bedrock with credentials only boto3 reads — is left for
    Python, untouched."""
    from db import async_session
    from db.models import ConfigSetting, Message

    dead = {"OLLAMA_HOST": f"http://127.0.0.1:{_free_port()}",
            # No keys or profile: a web identity token, which only boto3 reads.
            "AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": "", "AWS_PROFILE": "", "AWS_DEFAULT_PROFILE": "",
            "AWS_WEB_IDENTITY_TOKEN_FILE": str(work_dir / "token")}
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir, {**EDGE_ON, **dead})) as client:
        served = await _start(client, query="hello", model="ollama:llama3.3")
        not_served = await _start(client, query="hello", model="bedrock:us.anthropic.claude-sonnet-4-6")

        job = await _finished(served)
        assert (job.runtime, job.attempts, job.status) == ("edge", 1, "done")
        async with async_session() as s:
            message = await s.get(Message, served)
            assert message is not None and message.status == "error"
            assert message.content.startswith("The run failed before completing:")
        job = await _job(not_served)
        assert job is not None and (job.runtime, job.attempts, job.status) == (None, 0, "pending")

        # A configured MCP server is the edge's too: the turn stays here.
        async with async_session() as s:
            s.add(ConfigSetting(key="mcp.servers", value=json.dumps({"fs": {"command": "x"}})))
            await s.commit()
        with_mcp = await _start(client, query="hello", model="ollama:llama3.3")
        job = await _finished(with_mcp)
        assert (job.runtime, job.status) == ("edge", "done")

    # With the agent loop off, no turn is the edge's.
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir)) as client:
        async with async_session() as s:
            await s.delete(await s.get(ConfigSetting, "mcp.servers"))
            await s.commit()
        resp = await client.post("/graphql", json={"query": START, "variables": {"input": {"query": "x"}}})
        # Without a worker or the agent loop, starting a run is Python's.
        assert resp.status_code != 200 or "errors" in resp.json()


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

    # Serving: the dead edge's job is claimed again, and run here (its
    # model has no key in a test, so it fails — here).
    await seed("left-running")
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir, EDGE_ON)):
        job = await _finished("left-running")
        assert (job.runtime, job.attempts, job.status) == ("edge", 2, "done")

    # Not serving: every edge job goes to Python untouched.
    await seed("loop-off")
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _edge_env(work_dir)):
        job = await _settled("loop-off")
        assert (job.runtime, job.attempts) == (None, 1)


async def test_python_without_the_edge_adopts_its_jobs(database):
    from db import async_session
    from db.ops import adopt_edge_jobs

    async with async_session() as s:
        await _edge_job(s, "e-pending", status="pending")
        await _edge_job(s, "e-running", status="running", locked_by="edge-1")
        await _edge_job(s, "e-done", status="done")
        await s.commit()
        assert await adopt_edge_jobs(s) == 2
    for job_id, runtime in (("e-pending", None), ("e-running", None), ("e-done", "edge")):
        job = await _job(job_id)
        assert job is not None and job.runtime == runtime, job_id
