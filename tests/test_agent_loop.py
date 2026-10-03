"""The agent loop's own guarantees (`core/agent_loop.py`), beyond what the
golden logs pin: durability across a crash, converting a LangGraph thread on
first use, and how a run ends when something goes wrong."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from agent_harness import ModelCall, tool_call
from test_agent_golden import read_thread, run_turn, script  # noqa: F401 — `script` is a fixture

from core.agent_loop import Agent, DbThread, RecursionLimitReached, Run, Thread


async def test_a_reclaimed_turn_continues_where_it_stopped(jarvis, script):
    """A job that dies after a tool ran is claimed again: its prompt is not
    sent twice, the tool is not run twice, and the model sees the work so far."""
    from core.state import _tasks
    from server.chat_runtime import chat_job_handler

    runs = 0

    def respond(call: ModelCall) -> AIMessage:
        nonlocal runs
        if call.index == 0:
            return AIMessage(content="", tool_calls=[tool_call("write_todos", {"todos": ["A"]}, "c1")])
        if call.index == 1:
            raise RuntimeError("the process died here")
        runs += 1
        return AIMessage(content="Picked up.")

    script.responder = respond
    jobs: list[Any] = []
    original_claim = jarvis.queue.claim

    async def claim(**kwargs: Any) -> Any:
        jobs.append(await original_claim(**kwargs))
        return jobs[-1]

    jarvis.queue.claim = claim  # keep the job to hand it over again
    first = await run_turn(jarvis, script, "do the thing")
    assert first.message["status"] == "error"

    _tasks.pop(first.task_id, None)  # a restart forgets the run
    await chat_job_handler(jobs[0])

    thread = (await read_thread(first.conv_id))["messages"]
    assert [type(m).__name__ for m in thread] == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    assert sum(isinstance(m, HumanMessage) for m in thread) == 1
    resumed = script.calls[-1]
    assert [type(m).__name__ for m in resumed.messages if not m.type == "system"][:3] == [
        "HumanMessage", "AIMessage", "ToolMessage",
    ]
    assert runs == 1


async def test_a_checkpoint_only_thread_is_converted_on_first_use(jarvis):
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import empty_checkpoint

    from core.transcript_store import legacy_checkpointer

    history = [HumanMessage(content="old question", id="h1"), AIMessage(content="old answer", id="a1")]
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": history, "todos": [{"text": "kept", "status": "done"}]}
    config: RunnableConfig = {"configurable": {"thread_id": "conv-old", "checkpoint_ns": ""}}
    async with legacy_checkpointer(str(jarvis.config.checkpoints_db)) as cp:
        assert cp is None  # no checkpoints.db: nothing to convert from
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(jarvis.config.checkpoints_db)) as cp:
        await cp.aput(config, checkpoint, {}, {})

    thread = await DbThread.load("conv-old")
    assert thread.messages == history
    assert thread.todos == [{"text": "kept", "status": "done"}]
    # Converted once: the rows answer from now on, even with the file gone.
    Path(jarvis.config.checkpoints_db).unlink()
    again = await DbThread.load("conv-old")
    assert again.messages == history


def _looping_agent(tool_fn: Any) -> Agent:
    async def step(run: Run) -> list[Any]:
        n = sum(isinstance(m, AIMessage) for m in run.thread.messages)
        return [AIMessage(content="", tool_calls=[tool_call(tool_fn.name, {}, f"c{n}")])]

    return Agent("test", step, [tool_fn])


async def test_a_failing_tool_fails_the_run_with_the_call_on_record(database):
    @tool
    async def explode() -> str:
        """Fails."""
        raise RuntimeError("tool blew up")

    thread = DbThread("t-fail")
    with pytest.raises(RuntimeError, match="tool blew up"):
        await _looping_agent(explode).ainvoke({"messages": [("user", "go")]}, thread=thread)
    stored = await DbThread.load("t-fail")
    assert [type(m).__name__ for m in stored.messages] == ["HumanMessage", "AIMessage"]


async def test_the_step_limit(database):
    calls = 0

    @tool
    async def noop() -> str:
        """Does nothing."""
        nonlocal calls
        calls += 1
        return "ok"

    thread = Thread()
    with pytest.raises(RecursionLimitReached):
        await _looping_agent(noop).ainvoke({"messages": [("user", "go")]}, {"recursion_limit": 6}, thread=thread)
    # Six steps: three model calls, three tool batches.
    assert calls == 3
    assert sum(isinstance(m, ToolMessage) for m in thread.messages) == 3


async def test_closing_the_stream_cancels_the_run(database):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def step(run: Run) -> list[Any]:
        run.emit("custom", {"type": "hello"})
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return []

    agent = Agent("test", step, [])
    async with aclosing(agent.astream({"messages": [("user", "go")]}, thread=Thread())) as stream:
        async for _ in stream:
            break
    assert started.is_set()
    await asyncio.wait_for(cancelled.wait(), 1)
