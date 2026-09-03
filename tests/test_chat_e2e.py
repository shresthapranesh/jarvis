"""End-to-end chat: register -> enqueue -> claim -> handler -> stream -> persist.

This is the path every surface shares (GraphQL startTask, Telegram, Discord,
the board and automation runtimes all funnel through the same queue+TaskState
mechanics), so it is the highest-value single test in the suite.

The final leg calls a real model, so that test is marked `llm` and skipped
without credentials. Everything up to the LLM is exercised unconditionally.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


def _has_credentials() -> bool:
    import os

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    return bool(os.environ.get("GOOGLE_API_KEY"))


needs_llm = pytest.mark.skipif(
    not _has_credentials(), reason="no GOOGLE_API_KEY; set one to run live-model tests"
)


def _decode(raw: dict) -> tuple[str | None, dict]:
    """state.events entries are SSE-shaped: {"event": name, "data": json str}."""
    name = raw.get("event") or raw.get("type")
    field = raw.get("data")
    if isinstance(field, str):
        try:
            return name, json.loads(field)
        except json.JSONDecodeError:
            return name, {}
    return name, field or {}


async def test_register_creates_conversation_message_and_job(jarvis, work_dir: Path):
    """No model involved: registering must leave a conversation, a user
    message, a pre-registered TaskState, and a pending job whose id is the
    task id (the convention the single cancellation path depends on)."""
    from core.state import _tasks
    from db import async_session
    from db.models import Conversation, Message
    from server.chat_runtime import register_chat_task
    from sqlalchemy import select

    async with async_session() as s:
        dispatch = await register_chat_task(
            s, query="hello there", model="test:model"
        )
        await s.commit()
        task_id, conv_id = dispatch.task_id, dispatch.conversation_id

    assert task_id in _tasks, "TaskState must be registered before the commit"

    async with async_session() as s:
        conv = await s.get(Conversation, conv_id)
        assert conv is not None and conv.surface == "web"
        msgs = (await s.execute(
            select(Message).where(Message.conversation_id == conv_id)
        )).scalars().all()

    roles = [m.role for m in msgs]
    assert "user" in roles
    assert any(m.content == "hello there" for m in msgs)
    # The assistant placeholder is the task.
    assert any(m.id == task_id for m in msgs), "task_id should be the assistant Message id"

    job = await jarvis.queue.claim(kinds=["chat"], worker_id="test", ttl_seconds=60)
    assert job is not None, "a chat job should be pending"
    assert job.id == task_id, "job.id == task_id convention"
    assert job.payload["query"] == "hello there"


async def test_stream_task_events_yields_and_terminates(jarvis):
    """The subscription mechanic itself: appended events surface in order and
    the stream closes once the task is done."""
    from core.state import TaskState, _notify, _tasks, stream_task_events

    state = TaskState(kind="chat", label="t")
    _tasks["t1"] = state

    async def produce():
        for i in range(3):
            await asyncio.sleep(0.02)
            state.events.append({"event": "token", "data": f'{{"text": "t{i}"}}'})
            _notify(state)
        state.events.append({"event": "done", "data": '{"message": "fin"}'})
        state.done = True
        _notify(state)

    seen = []
    try:
        producer = asyncio.create_task(produce())
        async with asyncio.timeout(15):
            async for raw in stream_task_events(state):
                seen.append(_decode(raw)[0])
        await producer
    finally:
        _tasks.pop("t1", None)

    assert seen == ["token", "token", "token", "done"]


@needs_llm
async def test_full_chat_turn(jarvis, work_dir: Path):
    """The whole path with a live model: the reply streams and both messages
    land in the database as `done`."""
    from core.state import _tasks, stream_task_events
    from db import async_session
    from db.models import Message
    from db.ops import get_default_model
    from server.chat_runtime import chat_job_handler, register_chat_task
    from sqlalchemy import select

    async with async_session() as s:
        model = await get_default_model(s)
        dispatch = await register_chat_task(
            s, query="say hi in five words", model=model
        )
        await s.commit()
        task_id, conv_id = dispatch.task_id, dispatch.conversation_id

    job = await jarvis.queue.claim(kinds=["chat"], worker_id="test", ttl_seconds=600)
    assert job is not None and job.id == task_id
    run = asyncio.create_task(chat_job_handler(job))

    names, tokens = [], []
    async with asyncio.timeout(180):
        async for raw in stream_task_events(_tasks[task_id]):
            name, data = _decode(raw)
            names.append(name)
            if name == "token":
                tokens.append(data.get("text", ""))
    await run

    assert "done" in names, f"run did not finish cleanly: {names}"
    assert "error" not in names, f"run errored: {names}"
    assert "".join(tokens).strip(), "no assistant text streamed"

    async with async_session() as s:
        msgs = (await s.execute(
            select(Message).where(Message.conversation_id == conv_id)
            .order_by(Message.created_at)
        )).scalars().all()

    assert [m.role for m in msgs] == ["user", "assistant"]
    assert all(m.status == "done" for m in msgs), [m.status for m in msgs]
    assert msgs[1].content.strip(), "assistant message persisted empty"


@needs_llm
async def test_throughput_is_measured_and_persisted(jarvis, work_dir: Path):
    """Throughput has to survive the whole path, not just the callback.

    The unit tests drive `PerfCallbackHandler` directly with synthetic results;
    this is the only check that a real provider's stream actually fires
    `on_llm_new_token` (no TTFT boundary without it), that the handler is
    attached by whichever branch of the runtime built the callback list, and
    that the numbers reach the Message row.
    """
    from core.state import _tasks, stream_task_events
    from db import async_session
    from db.models import Message
    from db.ops import get_default_model
    from server.chat_runtime import chat_job_handler, register_chat_task
    from sqlalchemy import select

    async with async_session() as s:
        model = await get_default_model(s)
        # Long enough that generation spans well past _MIN_DECODE_SECONDS —
        # a one-line reply can legitimately arrive as a single buffered flush,
        # which reports prefill only and would make this assertion flaky.
        dispatch = await register_chat_task(
            s, query="List the numbers 1 through 60 separated by commas. No other text.",
            model=model,
        )
        await s.commit()
        task_id, conv_id = dispatch.task_id, dispatch.conversation_id

    job = await jarvis.queue.claim(kinds=["chat"], worker_id="test", ttl_seconds=600)
    assert job is not None
    run = asyncio.create_task(chat_job_handler(job))

    perf_events = []
    async with asyncio.timeout(180):
        async for raw in stream_task_events(_tasks[task_id]):
            name, data = _decode(raw)
            if name == "perf_update":
                perf_events.append(data)
    await run

    assert perf_events, "no perf_update event streamed"
    last = perf_events[-1]
    assert last["ttft_ms"] is not None and last["ttft_ms"] > 0
    assert last["eval_tps"] is not None and last["eval_tps"] > 0

    async with async_session() as s:
        assistant = (await s.execute(
            select(Message).where(Message.conversation_id == conv_id, Message.role == "assistant")
        )).scalars().one()

    ttft, llm_ms = assistant.ttft_ms, assistant.llm_ms
    assert ttft is not None and ttft > 0
    assert assistant.eval_tps is not None and assistant.eval_tps > 0
    # TTFT is part of the time spent inside LLM calls, so it can't exceed it.
    assert llm_ms is not None and llm_ms >= ttft
    # prefill_tps is allowed to be None (a fully cache-served prefill), but not
    # zero — that would mean the aggregate counted a span against no tokens.
    assert assistant.prefill_tps is None or assistant.prefill_tps > 0
