"""Framework-agnostic tool execution context — the PORT.

Tools call into this instead of reaching for LangGraph runtime primitives
(``get_config``, ``get_stream_writer``, ``adispatch_custom_event``,
``InjectedStore``/``InjectedState``, …) directly. A single adapter —
``current_ctx()`` — reads the ambient LangGraph runtime and builds the
context; it is the *only* place in the tools layer that imports LangGraph.

To move the tools onto a different agent library, rewrite ``current_ctx()``
(and add ports here for any new capability); the tool functions themselves
stay framework-free. The module has no top-level LangGraph import on purpose
— the dependency lives inside the one adapter function.
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
    raise RuntimeError("request_input is unavailable outside an agent run.")


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool needs from its runtime, with zero framework coupling.

    Attributes:
        conversation_id: DB conversation for this run, or None outside one
            (CLI / automation / workflow). Tools scope their work to it.
        thread_id: LangGraph thread for this run (present even when there is
            no conversation row).
        event_sink: where ``emit`` routes events; injected by ``current_ctx``.
    """

    conversation_id: str | None = None
    thread_id: str | None = None
    kernel_key: str | None = None
    # Set only while executing a board task (server/task_board_runtime.py);
    # complete_task/block_task refuse to run without it.
    board_task_id: str | None = None
    event_sink: EventSink = field(default=_noop_sink, repr=False)
    store: MemoryStore | None = None
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

        Mirrors the old ``get_stream_writer()({"type": ..., ...})`` /
        ``adispatch_custom_event`` calls — both land in the same ``custom``
        stream handler keyed on ``type`` (see ``core/streaming.py``).
        """
        try:
            self.event_sink({"type": event_type, **fields})
        except Exception as exc:  # a telemetry emit must never break a tool
            logger.debug("tool event emit failed (%s): %s", event_type, exc)

    def request_input(self, payload: Any) -> Any:
        """Suspend for human input (HITL), returning the answer on resume.

        Framework-agnostic wrapper over LangGraph's ``interrupt`` (wired by
        ``current_ctx``): first call raises to pause the run; on resume it
        returns the value the caller supplied. Used by the browser tool.
        """
        return self._request_input(payload)


def current_ctx() -> ToolContext:
    """Build a ToolContext from the ambient LangGraph runtime.

    THE adapter seam: the only function in the tools layer that touches
    LangGraph runtime APIs. Reads conversation_id / thread_id from the run
    config and wires the event sink to the stream writer. Safe to call
    anywhere — degrades to an empty context (no ids, no-op sink) when no run
    is active, so tools work in tests and non-streaming contexts too.
    """
    from langgraph.config import get_config, get_store, get_stream_writer
    from langgraph.types import interrupt

    conversation_id: Any = None
    thread_id: Any = None
    kernel_key: Any = None
    board_task_id: Any = None
    try:
        configurable = get_config().get("configurable") or {}
        conversation_id = configurable.get("conversation_id")
        thread_id = configurable.get("thread_id")
        kernel_key = configurable.get("kernel_key")
        board_task_id = configurable.get("board_task_id")
    except Exception:
        pass

    sink: EventSink = _noop_sink
    try:
        sink = get_stream_writer()  # writer(payload) writes to the custom stream
    except Exception:
        pass

    store: MemoryStore | None = None
    try:
        store = get_store()
    except Exception:
        pass

    return ToolContext(
        conversation_id=str(conversation_id) if conversation_id else None,
        thread_id=str(thread_id) if thread_id else None,
        kernel_key=str(kernel_key) if kernel_key else None,
        board_task_id=str(board_task_id) if board_task_id else None,
        event_sink=sink,
        store=store,
        _request_input=interrupt,
    )
