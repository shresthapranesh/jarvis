"""Agent builder — raw LangGraph StateGraph, code-first architecture."""

from __future__ import annotations

import logging
import sqlite3 as _sqlite3
from pathlib import Path
from typing import Annotated, NotRequired, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, RemoveMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.store.sqlite.aio import AsyncSqliteStore

from .config import get_config
from .model_catalog import (  # noqa: F401 — re-exported for backwards compat
    AVAILABLE_MODELS,
    DEFAULT_MODEL,
    ModelSpec,
    is_valid_model,
)
from tools.execute import execute
from tools.files import list_files, read_file, write_file
from tools.todos import write_todos
from tools.workers import set_worker_factory, spawn_workers
from tools.automations import (
    create_automation,
    delete_automation,
    list_automations,
    update_automation,
)
from tools.workflows import (
    create_workflow,
    delete_workflow,
    list_workflows,
    update_workflow,
)

logger = logging.getLogger(__name__)


# ── State schema ─────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    todos: NotRequired[list[str]]


# ── System prompt ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a powerful AI agent. Your primary action is execute(code) — Python that runs \
with full access to the network, filesystem, and all installed packages.

IMPORTANT: Call execute() directly and silently. Do NOT write code blocks or narrate \
what you are about to run in your response — the user sees your response text in real-time \
and showing raw code before results is noisy. Just make the tool call and then present the \
findings once you have results.

How to use execute() for common tasks:
  Web requests:       import httpx; r = httpx.get("https://..."); print(r.text[:5000])
  JS-rendered pages:  from playwright.sync_api import sync_playwright (chromium installed)
  Financial data:     import yfinance as yf; print(yf.Ticker("AAPL").fast_info)
  Data/analysis:      import pandas as pd, numpy as np
  Current date/time:  import datetime; print(datetime.datetime.now())
  Shell commands:     import subprocess; subprocess.run(["git", "log", "--oneline"])

For independent subtasks that can run in parallel, use spawn_workers([
  {"task": "...", "context": "optional background"},
  {"task": "..."},
]) — workers run concurrently and all results are returned when the last one finishes.

For files: read_file / write_file / list_files for simple access; \
or use pathlib inside execute().\
"""

# ── Thinking-block stripper (not persisted — runs at model-call time) ────────

_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


def _strip_thinking_from_message(msg: AIMessage) -> AIMessage:
    content = msg.content
    if not isinstance(content, list):
        return msg
    filtered = [
        b for b in content
        if not (isinstance(b, dict) and b.get("type") in _THINKING_TYPES)
    ]
    if len(filtered) == len(content):
        return msg
    if not filtered:
        filtered = [{"type": "text", "text": ""}]
    new_msg = msg.model_copy(update={"content": filtered})
    if "thinking" in (new_msg.additional_kwargs or {}):
        new_msg = new_msg.model_copy(update={
            "additional_kwargs": {k: v for k, v in new_msg.additional_kwargs.items() if k != "thinking"}
        })
    return new_msg


def _strip_historical_thinking(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Strip thinking blocks from all AIMessages except the most recent one."""
    last_ai = -1
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], AIMessage):
            last_ai = i
            break
    result: list[AnyMessage] = []
    for i, msg in enumerate(messages):
        if isinstance(msg, AIMessage) and i != last_ai:
            result.append(_strip_thinking_from_message(msg))
        else:
            result.append(msg)
    return result


# ── Summarization constants ───────────────────────────────────────────────────

_SUMMARIZE_THRESHOLD = 120_000  # chars (~30k tokens); trigger well before 200k context
_KEEP_RECENT = 10               # messages to keep verbatim


def _estimate_chars(messages: list[AnyMessage]) -> int:
    total = 0
    for m in messages:
        c = m.content
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict):
                    total += len(block.get("text", "") or block.get("thinking", ""))
    return total


# ── System message builder ───────────────────────────────────────────────────

def _make_system_message(text: str, cache: bool) -> SystemMessage:
    """Build a SystemMessage, optionally with an Anthropic cache breakpoint."""
    if cache:
        return SystemMessage(content=[{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}])
    return SystemMessage(text)


# ── Memory loading ────────────────────────────────────────────────────────────

async def _load_memory_from_store(store) -> str | None:
    """Read AGENTS.md from the AsyncSqliteStore."""
    try:
        item = await store.aget(("memory",), "AGENTS.md")
        if item is not None:
            return item.value.get("content", "").strip() or None
    except Exception as exc:
        logger.warning("could not read memory from store: %s", exc)
    return None


def _load_memory_from_disk() -> str | None:
    """Fallback for CLI mode (no store)."""
    try:
        path = Path(get_config().memory_file)
        if not path.is_absolute():
            path = Path(".") / path
        if path.exists():
            return path.read_text(encoding="utf-8").strip() or None
    except Exception as exc:
        logger.warning("could not load memory file: %s", exc)
    return None


# ── Checkpointer ─────────────────────────────────────────────────────────────

_sync_checkpointer: SqliteSaver | None = None


def _get_sync_checkpointer() -> SqliteSaver:
    global _sync_checkpointer
    if _sync_checkpointer is None:
        conn = _sqlite3.connect(get_config().checkpoints_db, check_same_thread=False)
        _sync_checkpointer = SqliteSaver(conn=conn)
    return _sync_checkpointer


# ── Agent builder ─────────────────────────────────────────────────────────────

def _build_agent(model: str, checkpointer, store: AsyncSqliteStore | None) -> CompiledStateGraph:
    spec = next((m for m in AVAILABLE_MODELS if m.id == model), None)
    if spec is None:
        raise ValueError(f"Unknown model '{model}'")
    llm = spec.build_llm()
    use_cache = spec.provider in ("bedrock", "anthropic")

    main_tools = [
        execute,
        read_file,
        write_file,
        list_files,
        write_todos,
        spawn_workers,
        list_automations,
        create_automation,
        update_automation,
        delete_automation,
        list_workflows,
        create_workflow,
        update_workflow,
        delete_workflow,
    ]

    # ── Graph nodes (closures capture llm, store, use_cache) ─────────────────

    def summarize_node(state: AgentState) -> dict:
        """Summarize old messages when conversation grows too long.

        Runs before every model call. Uses RemoveMessage to replace old messages
        in the checkpointer with a summary so we don't re-summarize each turn.
        """
        messages: list[AnyMessage] = list(state.get("messages", []))
        if _estimate_chars(messages) <= _SUMMARIZE_THRESHOLD:
            return {}
        to_summarize = messages[:-_KEEP_RECENT]
        if not to_summarize:
            return {}
        try:
            summary = llm.invoke([
                SystemMessage(
                    "Summarize the following conversation history concisely. "
                    "Preserve all key facts, decisions, tool outputs, and results."
                ),
                *to_summarize,
            ])
            summary_text = summary.content if isinstance(summary.content, str) else str(summary.content)
        except Exception as exc:
            logger.warning("summarization failed (%s) — skipping", exc)
            return {}
        logger.info("summarized %d messages into ~%d chars", len(to_summarize), len(summary_text))
        removals = [RemoveMessage(id=m.id) for m in to_summarize if hasattr(m, "id") and m.id]
        return {"messages": removals + [SystemMessage(f"[Prior conversation summary]\n{summary_text}")]}

    async def model_request_node(state: AgentState, config: RunnableConfig) -> dict:
        """Call the LLM with the current system message (memory + todos injected fresh)."""
        if store is not None:
            memory = await _load_memory_from_store(store)
        else:
            memory = _load_memory_from_disk()
        system = _SYSTEM_PROMPT
        if memory:
            system = f"{system}\n\n## Agent Memory\n\n{memory}"
        todos: list[str] = state.get("todos") or []
        if todos:
            todo_lines = "\n".join(f"- [ ] {t}" for t in todos)
            system = f"{system}\n\n## Current Tasks\n\n{todo_lines}"
        messages = _strip_historical_thinking(list(state.get("messages", [])))
        response = await llm.ainvoke(
            [_make_system_message(system, use_cache)] + messages,
            config=config,
        )
        return {"messages": [response]}

    # ── Build graph ───────────────────────────────────────────────────────────

    graph = StateGraph(AgentState)  # type: ignore[type-var]
    graph.add_node("summarize", summarize_node)
    graph.add_node("model_request", model_request_node)
    graph.add_node("tools", ToolNode(main_tools))

    # summarize runs before every model call (including after each tool round-trip)
    graph.add_edge(START, "summarize")
    graph.add_edge("summarize", "model_request")
    graph.add_conditional_edges("model_request", tools_condition)
    graph.add_edge("tools", "summarize")

    compiled = graph.compile(checkpointer=checkpointer, store=store, name="main")

    # ── Worker factory ────────────────────────────────────────────────────────

    worker_tools = [execute, read_file, write_file, list_files]

    def _make_worker():
        async def worker_model(state: AgentState, config: RunnableConfig) -> dict:
            response = await llm.ainvoke(list(state.get("messages", [])), config=config)
            return {"messages": [response]}

        worker_graph = StateGraph(AgentState)  # type: ignore[type-var]
        worker_graph.add_node("agent", worker_model)
        worker_graph.add_node("tools", ToolNode(worker_tools))
        worker_graph.add_edge(START, "agent")
        worker_graph.add_conditional_edges("agent", tools_condition)
        worker_graph.add_edge("tools", "agent")
        return worker_graph.compile(name="worker")

    set_worker_factory(_make_worker)
    return compiled


_cache: dict[tuple, CompiledStateGraph] = {}


def _build_cached(model: str, checkpointer, store) -> CompiledStateGraph:
    key = (model, id(checkpointer), id(store))
    if key not in _cache:
        _cache[key] = _build_agent(model, checkpointer, store)
    return _cache[key]


def build_agent(model: str = DEFAULT_MODEL, checkpointer=None, store: AsyncSqliteStore | None = None) -> CompiledStateGraph:
    """Build the agent. Defaults to sync SqliteSaver for CLI; server passes async variants."""
    if checkpointer is None:
        checkpointer = _get_sync_checkpointer()
    return _build_cached(model, checkpointer, store)
