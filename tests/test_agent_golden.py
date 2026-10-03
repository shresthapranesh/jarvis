"""Golden logs for the agent loop.

Each scenario runs a real chat turn — `register_chat_task` → queue →
`chat_job_handler` — with `tests/agent_harness.ScriptedChatModel` as the LLM,
and compares what the run did with `tests/golden/agent/<scenario>.json`, which
was recorded from the LangGraph runtime. The replacement loop has to reproduce
these: the events are what the frontend and the edge mirror consume, the Step
rows are the transcript after a reload, the model requests are the prompt
layout (and so the prompt cache), and the thread is what the next turn reads.

Re-record with `JARVIS_UPDATE_GOLDEN=1 uv run pytest tests/test_agent_golden.py`
— only for an intended change, and read the diff.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_harness import (
    ModelCall,
    Normalizer,
    Script,
    ScriptedChatModel,
    llm_call_record,
    message_record,
    normalize_events,
    tool_call,
    usage_events,
)

GOLDEN_DIR = Path(__file__).parent / "golden" / "agent"
UPDATE = os.environ.get("JARVIS_UPDATE_GOLDEN") == "1"

GOOGLE = "google_genai:gemma-4-31b-it"
ANTHROPIC = "anthropic:claude-sonnet-4-6"


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch):
    """The scripted model, installed as every catalog model's LLM."""
    from core import agents, tool_policy
    from core.model_catalog import ModelSpec

    holder = Script(responder=lambda call: AIMessage(content="(no responder)"))
    monkeypatch.setattr(ModelSpec, "build_llm", lambda self: ScriptedChatModel(script=holder))
    # Deterministic environment: no tool policy from disk, no live browser, and
    # an embedder that is "configured" (so the memory segment and `remember`
    # are present whatever keys this machine has) but never called.
    monkeypatch.setattr(tool_policy, "get_policies", lambda force=False: {})
    monkeypatch.setattr(agents, "_browser_reachable", lambda: False)
    monkeypatch.setattr(agents, "embeddings_available", lambda: True)

    async def _no_memories(query: str, k: int = 6) -> list:
        return []

    async def _no_core() -> str:
        return ""

    async def _no_episodes(conversation_id: str, query: str) -> list:
        return []

    monkeypatch.setattr(agents, "search_memory", _no_memories)
    monkeypatch.setattr(agents, "load_core", _no_core)
    monkeypatch.setattr("core.episodes.search_episodes", _no_episodes)
    agents.invalidate_agent_cache()
    agents._retrieval_cache.clear()
    yield holder
    agents.invalidate_agent_cache()


@dataclass
class Turn:
    task_id: str
    conv_id: str
    events: list[dict]
    steps: list[dict]
    message: dict
    calls: list[ModelCall] = field(default_factory=list)


async def run_turn(
    jarvis: Any, script: Script, query: str, *,
    model: str = GOOGLE, conv_id: str | None = None,
    watch: Callable[[str, Any], Any] | None = None,
) -> Turn:
    """One chat turn, start to finish. `watch(task_id, state)` runs alongside it."""
    from sqlalchemy import select

    from core.state import _tasks
    from db import async_session
    from db.models import Message, Step
    from server.chat_runtime import chat_job_handler, register_chat_task

    first_call = len(script.calls)
    async with async_session() as s:
        dispatch = await register_chat_task(s, query=query, model=model, conversation_id=conv_id)
        await s.commit()
    task_id = dispatch.task_id
    job = await jarvis.queue.claim(kinds=["chat"], worker_id="test", ttl_seconds=600)
    assert job is not None and job.id == task_id
    state = _tasks[task_id]
    watcher = asyncio.create_task(watch(task_id, state)) if watch else None
    try:
        async with asyncio.timeout(120):
            await chat_job_handler(job)
    finally:
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async with async_session() as s:
        steps = (await s.execute(
            select(Step).where(Step.message_id == task_id).order_by(Step.seq)
        )).scalars().all()
        msg = await s.get(Message, task_id)
        assert msg is not None
        message = {"content": msg.content, "status": msg.status}
    return Turn(
        task_id=task_id,
        conv_id=dispatch.conversation_id,
        events=list(state.events),
        steps=[
            {"node": st.node, "source": st.source, "subagent": st.subagent, "data": st.data}
            for st in steps
        ],
        message=message,
        calls=script.calls[first_call:],
    )


async def read_thread(thread_id: str) -> dict[str, Any]:
    """The thread as the next turn would load it, whichever runtime wrote it."""
    from core.transcript_store import load_thread
    from db import async_session

    async with async_session() as s:
        thread = await load_thread(s, thread_id)
    if thread.exists:
        return {"messages": thread.messages, "todos": thread.todos}
    from core.state import get_async_checkpointer

    tup = await get_async_checkpointer().aget_tuple(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    )
    values = (tup.checkpoint.get("channel_values") or {}) if tup else {}
    return {"messages": list(values.get("messages") or []), "todos": values.get("todos")}


async def record(turns: list[Turn], *, full_calls: bool = True) -> dict[str, Any]:
    norm = Normalizer()
    for i, t in enumerate(turns, 1):
        norm = Normalizer({**norm._map, t.conv_id: "<conversation>", t.task_id: f"<task{i}>"})
    out: dict[str, Any] = {"turns": []}
    for t in turns:
        out["turns"].append({
            "events": normalize_events(t.events, norm),
            "usage_events": usage_events(t.events),
            "steps": norm.value(t.steps),
            "message": norm.value(t.message),
            "llm_calls": [llm_call_record(c, norm, full=full_calls) for c in t.calls],
        })
    thread = await read_thread(turns[-1].conv_id)
    out["thread"] = {
        "messages": [message_record(m, norm) for m in thread["messages"]],
        "todos": thread["todos"],
    }
    return out


def check(name: str, got: dict[str, Any]) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    text = json.dumps(got, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    if UPDATE or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if not UPDATE:
            pytest.fail(f"recorded new golden {path.name}; re-run to compare")
        return
    want = json.loads(path.read_text(encoding="utf-8"))
    assert json.loads(text) == want, f"{name}: run differs from {path}"


def _last_tool_results(call: ModelCall) -> list[ToolMessage]:
    out: list[ToolMessage] = []
    for m in reversed(call.messages):
        if isinstance(m, ToolMessage):
            out.append(m)
        elif out:
            break
    return list(reversed(out))


# ── scenarios ────────────────────────────────────────────────────────────────


async def test_text_reply(jarvis, script):
    script.responder = lambda call: AIMessage(content="Hello there, friend.")
    turn = await run_turn(jarvis, script, "hi")
    check("text_reply", await record([turn]))


async def test_thinking_reply(jarvis, script):
    script.responder = lambda call: AIMessage(content=[
        {"type": "thinking", "thinking": "They said hi. "},
        {"type": "text", "text": "Hi back."},
    ])
    turn = await run_turn(jarvis, script, "hi")
    check("thinking_reply", await record([turn]))


async def test_todos(jarvis, script):
    def respond(call: ModelCall) -> AIMessage:
        if call.index == 0:
            return AIMessage(content="", tool_calls=[tool_call("write_todos", {"todos": ["Look", "Answer"]}, "c1")])
        if call.index == 1:
            return AIMessage(content="", tool_calls=[
                tool_call("set_todo_status", {"index": 0, "status": "done"}, "c2"),
            ])
        return AIMessage(content="All done.")

    script.responder = respond
    turn = await run_turn(jarvis, script, "plan then answer")
    check("todos", await record([turn]))


async def test_tool_errors(jarvis, script):
    """A batch with a good call, an unknown tool and bad arguments."""
    def respond(call: ModelCall) -> AIMessage:
        if call.index == 0:
            return AIMessage(content="Working. ", tool_calls=[
                tool_call("write_todos", {"todos": ["One"]}, "c1"),
                tool_call("no_such_tool", {"x": 1}, "c2"),
                tool_call("set_todo_status", {"index": "first", "status": "done"}, "c3"),
            ])
        return AIMessage(content="Recovered.")

    script.responder = respond
    turn = await run_turn(jarvis, script, "break things")
    check("tool_errors", await record([turn]))


async def test_worker(jarvis, script):
    def respond(call: ModelCall) -> AIMessage:
        if call.is_worker():
            return AIMessage(content="The worker's answer.")
        if call.index == 0:
            return AIMessage(content="", tool_calls=[tool_call(
                "spawn_workers", {"tasks": [{"task": "Summarise the thing", "role": "writer"}]}, "c1",
            )])
        return AIMessage(content="Workers finished.")

    script.responder = respond
    turn = await run_turn(jarvis, script, "delegate")
    check("worker", await record([turn]))


async def test_queued_message(jarvis, script):
    """A message typed mid-run reaches the next model call, not a new turn."""
    from db import async_session
    from server.chat_runtime import queue_chat_message

    task: dict[str, str] = {}

    async def respond(call: ModelCall) -> AIMessage:
        if call.index == 0:
            async with async_session() as s:
                await queue_chat_message(s, task["id"], "and one more thing")
                await s.commit()
            return AIMessage(content="", tool_calls=[tool_call("write_todos", {"todos": ["A"]}, "c1")])
        assert isinstance(call.messages[-2], HumanMessage) or "one more thing" in str(call.messages)
        return AIMessage(content="Got both.")

    async def watch(task_id: str, state: Any) -> None:
        task["id"] = task_id

    script.responder = respond
    turn = await run_turn(jarvis, script, "start", watch=watch)
    check("queued_message", await record([turn]))


async def test_recursion_limit(jarvis, script):
    """A model that never stops calling tools is cut off and finishes as done."""
    script.responder = lambda call: AIMessage(
        content=f"step {call.index} ", tool_calls=[tool_call("write_todos", {"todos": ["x"]}, f"c{call.index}")],
    )
    turn = await run_turn(jarvis, script, "loop forever")
    got = await record([turn], full_calls=False)
    # The full thread and event log are ~50 identical rounds; keep the shape.
    got["thread"]["messages"] = got["thread"]["messages"][:3] + got["thread"]["messages"][-3:]
    got["thread"]["n_messages"] = len((await read_thread(turn.conv_id))["messages"])
    events = got["turns"][0]["events"]
    got["turns"][0]["events"] = events[:8] + events[-8:]
    got["turns"][0]["n_events"] = len(events)
    steps = got["turns"][0]["steps"]
    got["turns"][0]["steps"] = steps[:4] + steps[-4:]
    got["turns"][0]["n_steps"] = len(steps)
    check("recursion_limit", got)


async def test_model_error(jarvis, script):
    def respond(call: ModelCall) -> AIMessage:
        raise RuntimeError("provider exploded")

    script.responder = respond
    turn = await run_turn(jarvis, script, "fail please")
    check("model_error", await record([turn]))


async def test_two_turns_cached(jarvis, script):
    """Turn two sees turn one, and the cached layout (Anthropic) holds."""
    script.responder = lambda call: AIMessage(content=f"Answer {call.index}.")
    first = await run_turn(jarvis, script, "first question", model=ANTHROPIC)
    second = await run_turn(jarvis, script, "second question", model=ANTHROPIC, conv_id=first.conv_id)
    check("two_turns_cached", await record([first, second]))


async def test_approval_denied(jarvis, script, monkeypatch: pytest.MonkeyPatch):
    from core import tool_policy
    from core.approvals import resolve
    from db import async_session

    monkeypatch.setattr(
        tool_policy, "get_policies",
        lambda force=False: {"bound:write_todos": tool_policy.ToolPolicy(approval=True)},
    )

    def respond(call: ModelCall) -> AIMessage:
        if call.index == 0:
            return AIMessage(content="", tool_calls=[tool_call("write_todos", {"todos": ["A"]}, "c1")])
        return AIMessage(content="Understood, skipping.")

    async def watch(task_id: str, state: Any) -> None:
        seen = 0
        while True:
            for raw in state.events[seen:]:
                if raw["event"] == "approval_request":
                    approval_id = json.loads(raw["data"])["approval_id"]
                    async with async_session() as s:
                        await resolve(s, approval_id, "no")
                        await s.commit()
                    return
            seen = len(state.events)
            await asyncio.sleep(0.02)

    script.responder = respond
    turn = await run_turn(jarvis, script, "make a plan", watch=watch)
    check("approval_denied", await record([turn]))
