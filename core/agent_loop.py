"""The agent loop: call the model, run the tools it asked for, repeat.

What LangGraph's `StateGraph` + checkpointer did for jarvis, written out:

- **History** is a `Thread`. A conversation's lives in the transcript tables
  (`core/transcript_store.py`) and is written a message at a time as the run
  goes — the model's reply, tool calls and all, before any tool runs; each tool
  result as soon as it and the calls before it are done. A one-shot run (a
  worker, a workflow node, the CLI) keeps an in-memory one.
- **One step** is one model call or one batch of tool calls. A run may take
  `recursion_limit` steps (LangGraph's meaning, so 100 is ~50 model calls)
  and then stops with `RecursionLimitReached`.
- **Output** is the chunk stream LangGraph's `astream(stream_mode=["updates",
  "messages", "custom"], subgraphs=True)` produced — `(namespace, mode, data)`
  — so `core/streaming._process_chunk` reads it unchanged. The namespace is
  always `()`: a worker's progress reaches the parent only through the events
  `spawn_workers` emits for it, never by leaking its own stream upward.

Tools see the run through `tools/context.current_ctx()`, which reads
`current_run()` — the contextvar set for the duration of a run.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, GenerationChunk, LLMResult
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import ensure_config, merge_configs, var_child_runnable_config
from langchain_core.tools.base import TOOL_MESSAGE_BLOCK_TYPES
from langchain_core.tracers._streaming import _StreamingCallbackHandler
from pydantic import ValidationError

from core.schemas import TodoItem, _normalise_todos, reduce_todos

logger = logging.getLogger(__name__)

StreamChunk = tuple[tuple[str, ...], str, Any]

# LangGraph's default, for a caller that sets none.
DEFAULT_RECURSION_LIMIT = 25


class RecursionLimitReached(RuntimeError):
    """The run used all `recursion_limit` steps without finishing."""


# ── threads ──────────────────────────────────────────────────────────────────


class Thread:
    """A run's history and todo list. In memory; `DbThread` adds the rows."""

    def __init__(self, messages: Sequence[BaseMessage] = (), todos: list[TodoItem] | None = None):
        self.messages: list[AnyMessage] = cast(list[AnyMessage], list(messages))
        self.todos: list[TodoItem] = _normalise_todos(todos)

    async def apply(self, updates: Sequence[Any]) -> list[BaseMessage]:
        """Merge messages in (`add_messages` semantics); returns them as written."""
        from core.transcript_store import merge_messages, prepare_messages

        incoming = prepare_messages(updates)
        self.messages = cast(list[AnyMessage], merge_messages(self.messages, incoming))
        await self._write(incoming)
        return incoming

    async def set_todos(self, todos: Any) -> list[TodoItem]:
        """Merge a todo list in (`reduce_todos`); returns the result."""
        self.todos = reduce_todos(self.todos, todos)
        await self._write_todos(self.todos)
        return self.todos

    async def _write(self, messages: list[BaseMessage]) -> None:
        return None

    async def _write_todos(self, todos: list[TodoItem]) -> None:
        return None


class DbThread(Thread):
    """A thread kept in the transcript tables."""

    def __init__(self, thread_id: str, messages: Sequence[BaseMessage] = (), todos: list[TodoItem] | None = None):
        super().__init__(messages, todos)
        self.thread_id = thread_id

    @classmethod
    async def load(cls, thread_id: str) -> "DbThread":
        """The thread's rows — converted from LangGraph's checkpoint first, if
        that is the only place it exists yet."""
        from core.transcript_store import import_checkpoint, legacy_checkpointer, load_thread
        from db import async_session

        async with async_session() as session:
            thread = await load_thread(session, thread_id)
            if not thread.exists:
                try:
                    async with legacy_checkpointer() as checkpointer:
                        converted = checkpointer is not None and await import_checkpoint(
                            session, checkpointer, thread_id,
                        )
                except Exception as exc:
                    # Either the batch conversion wrote it first (the unique
                    # keys refused this copy), or the checkpoint won't convert
                    # — which must not take the thread down with it: the run
                    # starts it fresh, and says so.
                    await session.rollback()
                    converted = True
                    if not (await load_thread(session, thread_id)).exists:
                        logger.warning("could not convert checkpoint thread %s: %s", thread_id, exc)
                        converted = False
                if converted:
                    thread = await load_thread(session, thread_id)
        return cls(thread_id, thread.messages, thread.todos)

    async def _write(self, messages: list[BaseMessage]) -> None:
        from core.transcript_store import apply_messages
        from db import async_session

        async with async_session() as session:
            await apply_messages(session, self.thread_id, messages)

    async def _write_todos(self, todos: list[TodoItem]) -> None:
        from core.transcript_store import set_todos
        from db import async_session

        async with async_session() as session:
            await set_todos(session, self.thread_id, list(todos))


# ── the run ──────────────────────────────────────────────────────────────────


@dataclass
class Run:
    """One invocation of an agent — what its steps and tools work against."""

    config: RunnableConfig
    thread: Thread
    store: Any = None
    _emit: Callable[[Any], None] = field(default=lambda _item: None, repr=False)

    @property
    def configurable(self) -> dict[str, Any]:
        return self.config.get("configurable") or {}

    def emit(self, mode: str, data: Any) -> None:
        self._emit(((), mode, data))

    async def step_done(self, node: str, messages: Sequence[BaseMessage]) -> None:
        """Report a finished step, and wait until the consumer has handled
        everything emitted so far.

        LangGraph didn't start the next step until the stream's reader came
        back for more, and readers rely on it: `_process_chunk` persists a
        step's row before announcing it, and an approval request (emitted
        straight onto the TaskState, not through this stream) must not
        overtake the step that asked for the tool.
        """
        self.emit("updates", {node: {"messages": list(messages)}})
        barrier = asyncio.get_running_loop().create_future()
        self._emit(_Barrier(barrier))
        await barrier

    def custom(self, payload: dict[str, Any]) -> None:
        """A tool's event (`ToolContext.emit`) — LangGraph's `custom` mode."""
        self.emit("custom", payload)

    def model_config(self) -> RunnableConfig:
        """The config for this run's model call: the run's, plus the handler
        that turns the model's streamed chunks into `messages` chunks."""
        return _with_handler(self.config, _TokenStream(self.emit))


class _Barrier:
    """Resolved by `Agent.astream` when its reader asks for the item after it."""

    __slots__ = ("future",)

    def __init__(self, future: asyncio.Future[None]):
        self.future = future


_current_run: contextvars.ContextVar[Run | None] = contextvars.ContextVar("jarvis_agent_run", default=None)


def current_run() -> Run | None:
    """The run whose step or tool is executing, or None outside one."""
    return _current_run.get()


def _with_handler(config: RunnableConfig, handler: BaseCallbackHandler) -> RunnableConfig:
    callbacks = config.get("callbacks")
    if callbacks is None:
        merged: Any = [handler]
    elif isinstance(callbacks, list):
        merged = [*callbacks, handler]
    else:  # a callback manager inherited from an enclosing run
        merged = callbacks.copy()
        # Inheritable: `with_retry` runs the model as a child run.
        merged.add_handler(handler, inherit=True)
    return {**config, "callbacks": merged}


class _TokenStream(BaseCallbackHandler, _StreamingCallbackHandler):
    """Model chunks → `("messages", (chunk, metadata))`.

    Being a `_StreamingCallbackHandler` is what makes `BaseChatModel.ainvoke`
    stream instead of waiting for the whole reply. A provider that doesn't
    stream produces no chunks; its whole reply is sent at the end instead, as
    LangGraph did.
    """

    run_inline = True

    def __init__(self, emit: Callable[[str, Any], None]):
        self._emit = emit
        self._streamed: set[Any] = set()

    def tap_output_aiter(self, run_id: Any, output: Any) -> Any:
        return output

    def tap_output_iter(self, run_id: Any, output: Any) -> Any:
        return output

    def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        if isinstance(chunk, ChatGenerationChunk):
            self._streamed.add(run_id)
            self._emit("messages", (chunk.message, {}))

    def on_llm_end(
        self, response: LLMResult, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any,
    ) -> None:
        if run_id in self._streamed:
            self._streamed.discard(run_id)
            return
        if response.generations and response.generations[0]:
            gen = response.generations[0][0]
            if isinstance(gen, ChatGeneration):
                self._emit("messages", (gen.message, {}))


# ── tools ────────────────────────────────────────────────────────────────────

# LangGraph ToolNode's wording, which models have seen in every thread so far.
_UNKNOWN_TOOL = "Error: {requested_tool} is not a valid tool, try one of [{available_tools}]."
_BAD_ARGS = (
    "Error invoking tool '{tool_name}' with kwargs {tool_kwargs} with error:\n"
    " {error}\n"
    " Please fix the error and try again."
)


def _tool_content(output: Any) -> Any:
    """ToolMessage content as ToolNode left it: text or content blocks as they
    are, anything else as JSON (or its str)."""
    import json

    if isinstance(output, str) or (
        isinstance(output, list)
        and all(isinstance(x, dict) and x.get("type") in TOOL_MESSAGE_BLOCK_TYPES for x in output)
    ):
        return output
    try:
        return json.dumps(output, ensure_ascii=False)
    except Exception:
        return str(output)


async def invoke_tool(tool: Any, call: dict[str, Any], config: RunnableConfig) -> ToolMessage:
    """Run one tool call. Bad arguments come back as an error result for the
    model to fix; any other exception propagates and fails the run, as it did
    under ToolNode."""
    try:
        result = await tool.ainvoke({**call, "type": "tool_call"}, config)
    except ValidationError as exc:
        error = "\n".join(
            f"{'.'.join(str(part) for part in err.get('loc', ()))}: {err.get('msg', 'Unknown error')}"
            for err in exc.errors()
        )
        return ToolMessage(
            _BAD_ARGS.format(tool_name=call["name"], tool_kwargs=call["args"], error=error),
            name=call["name"], tool_call_id=call["id"], status="error",
        )
    if not isinstance(result, ToolMessage):
        raise TypeError(f"Tool {call['name']} returned unexpected type: {type(result)}")
    result.content = _tool_content(result.content)
    return result


# A step that calls the model: given the run, returns the messages to add —
# whatever it folded in (compaction's removals and summary, queued user
# messages) and, last, the model's reply.
ModelStep = Callable[[Run], Awaitable[Sequence[BaseMessage]]]

# Decides which of a reply's tool calls may run: returns a ready result (a
# denial) for each call id that must not. None means every call runs.
ToolGate = Callable[[Run, list[dict[str, Any]]], Awaitable[dict[str, ToolMessage]]]


class Agent:
    """A model step and a toolset, run as a loop. Shared across runs (built
    once per model by `core/agents.build_agent`); everything per-run lives in
    the `Run`."""

    def __init__(
        self,
        name: str,
        model_step: ModelStep,
        tools: Sequence[Any],
        *,
        gate: ToolGate | None = None,
        store: Any = None,
    ):
        self.name = name
        self.model_step = model_step
        self.tools = list(tools)
        self.tools_by_name = {t.name: t for t in self.tools}
        self.gate = gate
        self.store = store

    # ── public ───────────────────────────────────────────────────────────────

    async def astream(
        self,
        input: dict[str, Any],
        config: RunnableConfig | None = None,
        *,
        stream_mode: Sequence[str] | None = None,
        subgraphs: bool = False,
        thread: Thread | None = None,
    ) -> AsyncGenerator[Any, None]:
        """Run to completion, yielding chunks as they happen.

        `input` is `{"messages": [...], "todos": [...]?}`, plus, for a turn
        taken over mid-run, `"resume": True` (run the thread's unanswered tool
        calls first) and `"steps_taken"`. Without `thread` the
        history is the database thread named by `configurable.thread_id`.
        Chunks are `(namespace, mode, data)` with `subgraphs=True`, else
        `(mode, data)`; `stream_mode` keeps only those modes. Closing the
        iterator early (`aclosing`, or `break` then garbage collection) cancels
        the run.
        """
        config = _effective_config(config)
        if thread is None:
            thread_id = (config.get("configurable") or {}).get("thread_id")
            if not thread_id:
                raise ValueError("an agent run needs a thread, or configurable.thread_id")
            thread = await DbThread.load(str(thread_id))

        queue: asyncio.Queue[Any] = asyncio.Queue()
        done = object()
        run = Run(config=config, thread=thread, store=self.store, _emit=queue.put_nowait)

        async def drive() -> None:
            _current_run.set(run)
            try:
                await self._run(run, input)
            finally:
                queue.put_nowait(done)

        task = asyncio.create_task(drive())
        modes = set(stream_mode) if stream_mode is not None else None
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                if isinstance(item, _Barrier):
                    if not item.future.done():
                        item.future.set_result(None)
                    continue
                ns, mode, data = item
                if modes is not None and mode not in modes:
                    continue
                yield item if subgraphs else (mode, data)
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def ainvoke(
        self, input: dict[str, Any], config: RunnableConfig | None = None, *, thread: Thread | None = None,
    ) -> dict[str, Any]:
        """Run to completion; returns `{"messages", "todos"}` as the thread ends.
        With neither `thread` nor a `thread_id`, the history is in memory."""
        if thread is None:
            thread_id = _thread_id(config)
            thread = await DbThread.load(thread_id) if thread_id else Thread()
        async for _ in self.astream(input, config, thread=thread):
            pass
        return {"messages": thread.messages, "todos": thread.todos}

    # ── the loop ─────────────────────────────────────────────────────────────

    async def _run(self, run: Run, input: dict[str, Any]) -> None:
        if "todos" in input:
            await run.thread.set_todos(input["todos"])
        if input.get("messages"):
            await run.thread.apply(input["messages"])

        limit = int(run.config.get("recursion_limit") or DEFAULT_RECURSION_LIMIT)
        # A run taken over from another runtime mid-turn counts the steps
        # that one already took against the same limit.
        steps = int(input.get("steps_taken") or 0)

        def take_step() -> None:
            nonlocal steps
            if steps >= limit:
                raise RecursionLimitReached(f"agent {self.name!r} reached its limit of {limit} steps")
            steps += 1

        if input.get("resume"):
            # Taken over mid-turn (the edge handed it here): the tool calls it
            # recorded but didn't run are run now, not repaired as orphans.
            # Only a handover sets this — a re-claim after a crash must never
            # run a tool twice.
            pending = unanswered_tool_calls(run.thread.messages)
            if pending:
                reply, calls = pending
                take_step()
                results = await self._run_tools(run, reply, calls)
                await run.step_done("tools", results)

        while True:
            take_step()
            update = await self.model_step(run)
            # Written before any tool runs: a run that dies mid-tool resumes
            # with the calls on record (their results get repaired), and never
            # runs a tool twice.
            await run.thread.apply(update)
            await run.step_done("model_request", update)
            update = list(update)
            reply = update[-1] if update else None
            if not isinstance(reply, AIMessage) or not reply.tool_calls:
                return
            take_step()
            results = await self._run_tools(run, reply)
            await run.step_done("tools", results)

    async def _run_tools(
        self, run: Run, reply: AIMessage, calls: list[dict[str, Any]] | None = None,
    ) -> list[ToolMessage]:
        calls = [dict(c) for c in (reply.tool_calls if calls is None else calls)]
        ready = await self.gate(run, calls) if self.gate is not None else {}

        async def one(call: dict[str, Any]) -> ToolMessage:
            if call.get("id") in ready:
                return ready[call["id"]]
            tool = self.tools_by_name.get(call["name"])
            if tool is None:
                return ToolMessage(
                    _UNKNOWN_TOOL.format(requested_tool=call["name"], available_tools=", ".join(self.tools_by_name)),
                    name=call["name"], tool_call_id=call["id"], status="error",
                )
            return await invoke_tool(tool, call, run.config)

        tasks = [asyncio.ensure_future(one(call)) for call in calls]
        results: list[ToolMessage] = []
        try:
            # Concurrent, but recorded in call order: each result is written as
            # soon as it and every call before it have finished.
            for task in tasks:
                result = await task
                await run.thread.apply([result])
                results.append(result)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return results


def unanswered_tool_calls(messages: Sequence[BaseMessage]) -> tuple[AIMessage, list[dict[str, Any]]] | None:
    """The thread's last model reply and those of its tool calls with no
    result yet, when the thread ends on that reply's tool batch."""
    answered: set[str] = set()
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            answered.add(message.tool_call_id)
            continue
        if isinstance(message, AIMessage) and message.tool_calls:
            calls = [dict(c) for c in message.tool_calls if c.get("id") not in answered]
            return (message, calls) if calls else None
        return None
    return None


def _thread_id(config: RunnableConfig | None) -> str:
    return str(((config or {}).get("configurable") or {}).get("thread_id") or "")


def _effective_config(config: RunnableConfig | None) -> RunnableConfig:
    """The run's config, merged into the enclosing run's when there is one.

    A worker runs inside its parent's `spawn_workers` call. LangGraph merged
    the two, so the parent's callbacks (budget, perf) count the worker's model
    calls too, and the parent's ids reach the worker's tools unless the worker
    sets its own.
    """
    parent = var_child_runnable_config.get()
    if parent:
        return ensure_config(merge_configs(parent, config))
    return ensure_config(config)
