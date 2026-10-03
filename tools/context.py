"""Framework-agnostic tool execution context — the PORT.

Tools call into this for what they need from the run they're part of — its
ids, the live event stream, the todo list — instead of reaching into the agent
loop. ``current_ctx()`` builds the context from ``core.agent_loop.current_run()``
and is the only place in the tools layer that knows how runs work; the tool
functions themselves stay loop-free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

# An event sink takes a payload dict (which MUST carry a "type" key) and routes
# it to the live stream. Defaults to a no-op so tools invoked outside an agent
# run (tests, direct calls, CLI without streaming) never crash on emit.
EventSink = Callable[[dict[str, Any]], None]


def _noop_sink(_payload: dict[str, Any]) -> None:
    return


class MemoryStore(Protocol):
    """Minimal async key-value store — what the memory-routing file tools need.

    Mirrors the slice of LangGraph's ``BaseStore`` the tools actually use,
    without importing it, so ``files.py`` stays framework-free. ``aget``
    returns an item whose ``.value`` is the stored dict, or ``None``.
    """

    async def aget(self, namespace: tuple[str, ...], key: str) -> Any: ...
    async def aput(self, namespace: tuple[str, ...], key: str, value: dict[str, Any]) -> None: ...


def _no_input(_payload: Any) -> Any:
    raise RuntimeError("a run cannot be suspended for input.")


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool needs from its runtime, with zero framework coupling.

    Attributes:
        conversation_id: DB conversation for this run, or None outside one
            (CLI / automation / workflow). Tools scope their work to it.
        thread_id: the run's thread (present even when there is no
            conversation row).
        event_sink: where ``emit`` routes events; injected by ``current_ctx``.
    """

    conversation_id: str | None = None
    thread_id: str | None = None
    kernel_key: str | None = None
    # The assistant Message row this run is writing (chat only — task_id ==
    # message id). Artifacts stamp it so the UI can render each one under the
    # message that produced it; None outside chat (automation/board/CLI).
    message_id: str | None = None
    # Set only while executing a board task (server/task_board_runtime.py);
    # complete_task/block_task refuse to run without it.
    board_task_id: str | None = None
    # Set only when the conversation belongs to a project (server/chat_runtime.py);
    # project_memory refuses to run without it.
    project_id: str | None = None
    # True for incognito conversations (server/chat_runtime.py): tools that write
    # long-term state (remember, ...) turn themselves into no-ops so nothing
    # outlives the ephemeral conversation.
    ephemeral: bool = False
    event_sink: EventSink = field(default=_noop_sink, repr=False)
    store: MemoryStore | None = None
    # The run's thread, for the todo tools (`todos` / `set_todos`); None
    # outside a run.
    thread: Any = field(default=None, repr=False)
    _request_input: Callable[[Any], Any] = field(default=_no_input, repr=False)

    @property
    def session_key(self) -> str | None:
        """Identity of the run: conversation_id if present, else thread_id."""
        return self.conversation_id or self.thread_id

    @property
    def code_session_key(self) -> str | None:
        """Kernel scope for run_cell — an explicit kernel_key overrides identity.

        Lets parallel workers each get an isolated kernel (unique kernel_key)
        while their other tools still scope to the parent conversation.
        """
        return self.kernel_key or self.session_key

    def emit(self, event_type: str, **fields: Any) -> None:
        """Push a custom stream event to the live UI. No-op off-run.

        Lands in the run's ``custom`` stream, keyed on ``type`` (see
        ``core/streaming.py``).
        """
        try:
            self.event_sink({"type": event_type, **fields})
        except Exception as exc:  # a telemetry emit must never break a tool
            logger.debug("tool event emit failed (%s): %s", event_type, exc)

    def request_input(self, payload: Any) -> Any:
        """Suspend for human input. No run can be suspended mid-tool (the
        LangGraph interrupt is gone), so this raises — `request_tool_approval`
        reads that as a denial. A gated tool asks through `core/tool_gate`."""
        return self._request_input(payload)


def current_ctx() -> ToolContext:
    """The ToolContext of the run this code is executing in.

    THE adapter seam: reads the ids from the run's config and wires the event
    sink to the run's stream. Safe to call anywhere — degrades to an empty
    context (no ids, no-op sink) outside a run, so tools work in tests and
    non-streaming contexts too.
    """
    from core.agent_loop import current_run

    run = current_run()
    if run is None:
        return ToolContext()
    configurable = run.configurable

    def _s(key: str) -> str | None:
        value = configurable.get(key)
        return str(value) if value else None

    return ToolContext(
        conversation_id=_s("conversation_id"),
        thread_id=_s("thread_id"),
        kernel_key=_s("kernel_key"),
        message_id=_s("message_id"),
        board_task_id=_s("board_task_id"),
        project_id=_s("project_id"),
        ephemeral=bool(configurable.get("ephemeral", False)),
        event_sink=run.custom,
        store=run.store,
        thread=run.thread,
    )
