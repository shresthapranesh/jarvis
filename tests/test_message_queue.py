"""Mid-run message queue: queue -> drain -> deliver, and what happens when the
run ends before the drain does.

A message typed while a run is in flight must not become a second run (two runs
on one conversation share a LangGraph thread_id and race the checkpointer), and
must not be lost if the run it was queued against dies. Both carriers are
checked here: `TaskState.pending_input` (the fast path the graph node reads)
and the `messages` row with status `queued` (what survives a restart).
"""

from __future__ import annotations

import json

import pytest


def _events(state, name: str) -> list[dict]:
    out = []
    for raw in state.events:
        if (raw.get("event") or raw.get("type")) == name:
            data = raw.get("data")
            out.append(json.loads(data) if isinstance(data, str) else (data or {}))
    return out


async def _row(session, message_id: str):
    """The message row, asserted present — `session.get` is Optional-typed."""
    from db.models import Message

    row = await session.get(Message, message_id)
    assert row is not None, f"message {message_id} is gone"
    return row


async def _running_task(query: str = "first turn") -> tuple[str, str]:
    """Register a chat task and leave it in flight (no worker claims it)."""
    from db import async_session
    from server.chat_runtime import register_chat_task

    async with async_session() as s:
        dispatch = await register_chat_task(s, query=query, model="test:model")
        await s.commit()
        task_id, conv_id = dispatch.task_id, dispatch.conversation_id
    return task_id, conv_id


async def test_queue_writes_row_and_state(jarvis):
    from core.state import _tasks
    from db import async_session
    from db.models import Message
    from server.chat_runtime import queue_chat_message

    task_id, conv_id = await _running_task()
    state = _tasks[task_id]

    async with async_session() as s:
        msg_id, position = await queue_chat_message(s, task_id, "  also check the tests  ")

    assert position == 1
    # Fast path: readable by the graph node with no IO.
    assert [m.id for m in state.pending_input] == [msg_id]
    assert state.pending_input[0].text == "also check the tests"

    # Durable path: a real user row, marked queued so it can be told apart
    # from an ordinary turn after a restart.
    async with async_session() as s:
        row = await _row(s, msg_id)
    assert (row.conversation_id, row.role, row.status) == (conv_id, "user", "queued")

    assert _events(state, "queued_message") == [
        {"message_id": msg_id, "text": "also check the tests", "position": 1}
    ]


async def test_drain_delivers_human_messages_and_clears_the_queue(jarvis):
    from core.agents import _drain_queued_input
    from core.state import _tasks
    from db import async_session
    from db.models import Message
    from server.chat_runtime import queue_chat_message

    task_id, _ = await _running_task()
    state = _tasks[task_id]
    async with async_session() as s:
        first, _ = await queue_chat_message(s, task_id, "one")
        second, _ = await queue_chat_message(s, task_id, "two")

    # message_id is the task id in the chat config (chat_runtime builds it that
    # way); the node has nothing else to look the run up by.
    delivered = await _drain_queued_input({"configurable": {"message_id": task_id}})

    assert [m.content for m in delivered] == ["one", "two"]
    # The row id rides along as the HumanMessage id: it is the retrieval-cache
    # key the queue resolver warmed, and makes a node replay idempotent.
    assert [m.id for m in delivered] == [first, second]
    assert state.pending_input == []

    async with async_session() as s:
        assert (await _row(s, first)).status == "delivered"
        assert (await _row(s, second)).status == "delivered"

    assert _events(state, "queued_consumed") == [{"message_ids": [first, second]}]

    # Draining again yields nothing — no double delivery.
    assert await _drain_queued_input({"configurable": {"message_id": task_id}}) == []


async def test_drain_is_inert_without_a_queue_or_a_task(jarvis):
    from core.agents import _drain_queued_input

    task_id, _ = await _running_task()
    assert await _drain_queued_input({}) == []
    assert await _drain_queued_input({"configurable": {}}) == []
    assert await _drain_queued_input({"configurable": {"message_id": "nope"}}) == []
    assert await _drain_queued_input({"configurable": {"message_id": task_id}}) == []


async def test_unqueue_removes_row_and_entry(jarvis):
    from core.state import _tasks
    from db import async_session
    from db.models import Message
    from server.chat_runtime import queue_chat_message, unqueue_chat_message

    task_id, _ = await _running_task()
    state = _tasks[task_id]
    async with async_session() as s:
        msg_id, _ = await queue_chat_message(s, task_id, "never mind")
        assert await unqueue_chat_message(s, task_id, msg_id) is True

    assert state.pending_input == []
    async with async_session() as s:
        assert await s.get(Message, msg_id) is None

    # Already gone (or already delivered) — reported, not raised.
    async with async_session() as s:
        assert await unqueue_chat_message(s, task_id, msg_id) is False


async def test_queue_refused_when_the_run_cannot_reach_a_drain(jarvis):
    from core.state import InterruptRequest, _tasks
    from db import async_session
    from server.chat_runtime import queue_chat_message

    task_id, _ = await _running_task()
    state = _tasks[task_id]

    async with async_session() as s:
        with pytest.raises(ValueError, match="empty message"):
            await queue_chat_message(s, task_id, "   ")

        # A paused run is waiting for *this* answer and will not reach the
        # drain until it gets one; resumeTask is that path.
        state.set_interrupt(InterruptRequest(id="i1", question="ok?"))
        with pytest.raises(ValueError, match="waiting on an answer"):
            await queue_chat_message(s, task_id, "hi")
        state.clear_interrupt()

        state.done = True
        with pytest.raises(ValueError, match="already finished"):
            await queue_chat_message(s, task_id, "hi")

        with pytest.raises(ValueError, match="not found"):
            await queue_chat_message(s, "no-such-task", "hi")


async def test_leftovers_are_adopted_by_the_next_run(jarvis):
    """A run that stops with a queue leaves rows behind; the next run on the
    conversation delivers them. Same path a restart takes."""
    from core.state import TaskState, _tasks
    from db import async_session
    from server.chat_runtime import _adopt_queued_messages, queue_chat_message

    task_id, conv_id = await _running_task()
    async with async_session() as s:
        first, _ = await queue_chat_message(s, task_id, "one")
        second, _ = await queue_chat_message(s, task_id, "two")

    # The run dies without draining — TaskState is gone, rows are not.
    _tasks.pop(task_id)

    successor = TaskState(kind="chat", label="next", parent_id=conv_id)
    await _adopt_queued_messages(conv_id, successor)
    assert [m.id for m in successor.pending_input] == [first, second]
    assert [m.text for m in successor.pending_input] == ["one", "two"]

    # Idempotent: a second adoption pass must not double up.
    await _adopt_queued_messages(conv_id, successor)
    assert len(successor.pending_input) == 2


async def test_redispatch_starts_a_new_turn_from_the_leftovers(jarvis):
    from core.state import _tasks
    from db import async_session
    from db.models import Job, Message
    from server.chat_runtime import _redispatch_queued, queue_chat_message
    from sqlalchemy import select

    task_id, conv_id = await _running_task()
    state = _tasks[task_id]
    async with async_session() as s:
        first, _ = await queue_chat_message(s, task_id, "one")
        second, _ = await queue_chat_message(s, task_id, "two")

    await _redispatch_queued(state, conv_id, "test:model")

    async with async_session() as s:
        jobs = (await s.execute(select(Job).where(Job.kind == "chat"))).scalars().all()
        # The original run's job plus the one this dispatched.
        new = [j for j in jobs if j.id != task_id]
        assert len(new) == 1
        payload = json.loads(new[0].payload)
        assert payload["query"] == "one"
        assert payload["conv_id"] == conv_id

        # The promoted message opens its own turn, so it is an ordinary `done`
        # row — not the `delivered` a mid-run handoff records. The rest stay
        # queued for the new run to adopt.
        assert (await _row(s, first)).status == "done"
        assert (await _row(s, second)).status == "queued"

    assert state.pending_input == []


async def test_delivered_status_survives_for_the_thread_to_order_on(jarvis):
    """`delivered` is not decoration. The assistant row is stamped at run start,
    so a mid-run message is created *after* the reply it lands in; the status is
    the only thing that distinguishes it from an ordinary next-turn prompt when
    the thread re-sorts them."""
    from core.agents import _drain_queued_input
    from core.state import _tasks
    from db import async_session
    from db.models import Message
    from server.chat_runtime import queue_chat_message

    task_id, conv_id = await _running_task()
    async with async_session() as s:
        msg_id, _ = await queue_chat_message(s, task_id, "mid-run")
    await _drain_queued_input({"configurable": {"message_id": task_id}})

    async with async_session() as s:
        row = await _row(s, msg_id)
        assistant = await _row(s, task_id)
    assert row.status == "delivered"
    # The ordering problem the status exists to fix, stated as a fact.
    assert row.created_at > assistant.created_at


async def test_redispatch_is_a_noop_without_leftovers(jarvis):
    from core.state import _tasks
    from db import async_session
    from db.models import Job
    from server.chat_runtime import _redispatch_queued
    from sqlalchemy import select

    task_id, conv_id = await _running_task()
    await _redispatch_queued(_tasks[task_id], conv_id, "test:model")
    async with async_session() as s:
        jobs = (await s.execute(select(Job).where(Job.kind == "chat"))).scalars().all()
    assert [j.id for j in jobs] == [task_id]


# ── startTask routing ────────────────────────────────────────────────────────
#
# Starting a second turn on a conversation that is already running is not a
# heavier version of the same request: both runs share the conversation's
# LangGraph thread_id and race the checkpointer. Every surface routes through
# one rule instead.


async def test_start_task_queues_onto_the_live_run(jarvis):
    from core.state import _tasks
    from db import async_session
    from db.models import Job, Message
    from server.chat_runtime import register_chat_task
    from sqlalchemy import select

    task_id, conv_id = await _running_task("first turn")

    async with async_session() as s:
        second = await register_chat_task(
            s, query="second turn", model="test:model", conversation_id=conv_id,
        )
        await s.commit()

    assert second.queued is True
    # The caller is handed the *running* task, so it subscribes to the stream
    # that will actually answer it.
    assert second.task_id == task_id
    assert [m.id for m in _tasks[task_id].pending_input] == [second.queued_message_id]

    async with async_session() as s:
        jobs = (await s.execute(select(Job).where(Job.kind == "chat"))).scalars().all()
        row = await _row(s, second.queued_message_id or "")
    assert [j.id for j in jobs] == [task_id], "no second run may be enqueued"
    assert (row.role, row.status, row.content) == ("user", "queued", "second turn")


async def test_start_task_starts_normally_when_idle(jarvis):
    from core.state import _tasks
    from db import async_session
    from server.chat_runtime import register_chat_task

    task_id, conv_id = await _running_task("first turn")
    _tasks[task_id].done = True  # the run finished

    async with async_session() as s:
        second = await register_chat_task(
            s, query="second turn", model="test:model", conversation_id=conv_id,
        )
        await s.commit()

    assert second.queued is False
    assert second.task_id != task_id
    assert second.conversation_id == conv_id


async def test_start_task_refuses_to_queue_attachments(jarvis):
    from core.schemas import AttachmentIn
    from db import async_session
    from server.chat_runtime import register_chat_task

    _, conv_id = await _running_task("first turn")
    att = AttachmentIn(type="image", name="a.png", mime_type="image/png", data="", size=0)

    async with async_session() as s:
        with pytest.raises(ValueError, match="attachments cannot be"):
            await register_chat_task(
                s, query="look at this", model="test:model",
                conversation_id=conv_id, attachments=[att],
            )


async def test_in_flight_lookup_is_scoped_to_the_conversation(jarvis):
    from core.state import _tasks
    from server.chat_runtime import in_flight_chat_task

    task_id, conv_id = await _running_task("first turn")
    assert in_flight_chat_task(conv_id) == task_id
    assert in_flight_chat_task("some-other-conversation") is None

    # A finished run lingers in the registry for a few seconds so the UI can
    # show its terminal state; it must not look like a run to queue onto.
    _tasks[task_id].done = True
    assert in_flight_chat_task(conv_id) is None
