"""Agent builder — raw LangGraph StateGraph, code-first architecture."""

from __future__ import annotations

import logging
import sqlite3 as _sqlite3
from pathlib import Path
from typing import Annotated, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.store.sqlite.aio import AsyncSqliteStore

from .config import get_config
from .messages import (
    build_llm_messages,
    repair_orphan_tool_calls,
    strip_historical_thinking,
)
from .model_catalog import (  # noqa: F401 — re-exported for backwards compat
    DEFAULT_MODEL,
    ModelSpec,
    get_model_spec,
    is_valid_model,
)
from .safety import make_safe_execute, make_safe_write_artifact, make_safe_write_file
from .schemas import TodoItem, _normalise_todos, reduce_todos
from .summarization import maybe_summarize
from tools.artifacts import list_artifacts as artifact_list, read_artifact
from tools.documents import read_document, search_documents
from tools.files import list_files, read_file
from tools.todos import set_todo_status, write_todos
from tools.workers import make_spawn_workers
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


# ── Transient LLM error retry ────────────────────────────────────────────────
# Upstream providers occasionally return 5xx / transient network errors mid-run
# (e.g. Google's "500 Internal error encountered" on Gemma). One automatic retry
# absorbs those without surfacing a hard failure to the user. We catch only the
# specific transient subclasses — never the broad APIError/Exception — so real
# 4xx-class problems (bad input, context overflow, auth) still fail fast.

def _collect_transient_errors() -> tuple[type[BaseException], ...]:
    classes: list[type[BaseException]] = []
    try:
        from google.genai.errors import ServerError as _GenaiServerError
        classes.append(_GenaiServerError)
    except ImportError:
        pass
    try:
        from google.api_core.exceptions import (
            InternalServerError as _GApiInternal,
            ServiceUnavailable as _GApiUnavailable,
            DeadlineExceeded as _GApiDeadline,
            GatewayTimeout as _GApiGateway,
        )
        classes.extend([_GApiInternal, _GApiUnavailable, _GApiDeadline, _GApiGateway])
    except ImportError:
        pass
    try:
        from anthropic import (
            APIConnectionError as _AnthroConn,
            APITimeoutError as _AnthroTimeout,
            InternalServerError as _AnthroInternal,
            RateLimitError as _AnthroRate,
        )
        classes.extend([_AnthroConn, _AnthroTimeout, _AnthroInternal, _AnthroRate])
    except ImportError:
        pass
    return tuple(classes)


_TRANSIENT_LLM_ERRORS: tuple[type[BaseException], ...] = _collect_transient_errors()


def _with_llm_retry(runnable):
    """Wrap an LLM Runnable with one automatic retry on transient upstream errors.

    stop_after_attempt=2 = original + 1 retry. We stay conservative because a
    retry that fires after partial token streaming will re-emit those tokens to
    the user; keeping it at one retry caps the visible blast radius while still
    absorbing the common case (server returns 5xx before generating anything).
    """
    if not _TRANSIENT_LLM_ERRORS:
        return runnable
    return runnable.with_retry(
        retry_if_exception_type=_TRANSIENT_LLM_ERRORS,
        stop_after_attempt=2,
        wait_exponential_jitter=True,
    )


# ── State schema ─────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    todos: NotRequired[Annotated[list[TodoItem], reduce_todos]]


# ── System prompt ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a powerful AI agent. Your primary action is execute(code) — Python that runs \
with full access to the network, filesystem, and all installed packages.

## Output rules
- Only write text when giving your FINAL answer to the user.
- While working (calling tools, analyzing results), write NOTHING. Call tools silently.
- Do NOT write "Thought:", "Action:", "Observation:", or any narration of your process.
- Do NOT paste code in your response — call execute() directly.
- The user sees a live activity feed of your tool calls; they do not need a running commentary.

## execute() is stateless
Every execute() call runs in a **fresh subprocess**. Variables, imports, open files, and \
in-memory data do NOT persist between calls. Batch related work into one call rather \
than splitting it across many. Re-import packages and re-fetch data each time.

## How to work
1. Call execute() to get data or run computation.
2. Examine the output — check for errors, gaps, missing info.
3. If needed, call execute() again to dig deeper or fix issues. Stay silent while doing this.
4. When you have a complete, verified answer, write it clearly as your response.

## Common patterns
  Web requests:       import httpx; r = httpx.get("https://..."); print(r.text[:5000])
  JS-rendered pages:  from playwright.sync_api import sync_playwright (chromium installed)
  Financial data:     import yfinance as yf; print(yf.Ticker("AAPL").fast_info)
  Data/analysis:      import pandas as pd, numpy as np
  Current date/time:  import datetime; print(datetime.datetime.now())
  Shell commands:     import subprocess; subprocess.run(["git", "log", "--oneline"])

For independent subtasks that can run in parallel, use spawn_workers. Each task \
takes an optional `role` — pick the most specific fitting one:
  - "researcher" — finds and verifies information from the web / source material
  - "coder"      — writes or modifies code (read, edit, run, iterate)
  - "writer"     — produces final-quality prose (no execute, file ops only)
  - "general"    — fallback when nothing else fits (full toolset)

Example:
  spawn_workers([
    {"role": "researcher", "task": "Find current US, China, and EU GDP"},
    {"role": "researcher", "task": "Find current US, China, and EU population"},
    {"role": "writer", "task": "Draft a one-paragraph comparison from {data}"},
  ])

Workers run concurrently and all results are returned when the last one finishes.

For files: read_file / write_file / list_files for simple access; \
or use pathlib inside execute().

## Planning long-running work
For any task that needs more than ~3 tool calls, call `write_todos` once at the \
start with the steps you intend to take. As you work, call \
`set_todo_status(index, "in_progress")` before starting an item and \
`set_todo_status(index, "done")` after finishing it. The user sees this list \
update live, so it doubles as your status report. Skip the todo list entirely \
for one-shot questions — keep it for genuinely multi-step work.

## Attached documents
Small attached documents appear inline in the message. Large ones are indexed \
instead — the message carries a stub with a document_id. For those, call \
`search_documents(query)` to find the passages you need (phrase the query as \
the content you're looking for), and `read_document(document_id, offset)` to \
read sequentially. Never answer questions about an indexed document from \
memory — search it first.

## Artifacts (deliverables)
When the user asks for a finished document — a report, draft, brief, resume, \
plan, summary write-up, etc. — call `write_artifact(title, content)` instead \
of `write_file`. Artifacts open in the user's side panel where they can read, \
edit, copy, and download them; scratch files do not. To revise an existing \
artifact, pass the `artifact_id` returned from a prior call. Use `read_artifact` \
to load one back, and `list_artifacts` to see what already exists. Don't paste \
the full artifact body into your final reply — a one-line confirmation referring \
to the artifact title is enough; the user can already see it.\
"""

# ── Worker-role prompts ───────────────────────────────────────────────────────
# Each role gets a tuned prompt and (inside _build_agent) a tool subset. The
# worker subgraph compiles with `name=role`, so LangGraph's namespace surfaces
# the role to the streaming layer (which already labels by subagent name).

_ROLE_PROMPTS = {
    "general": (
        "You are a focused worker agent. Complete the task given to you using "
        "execute(code) — Python with full network/filesystem access. Each execute() "
        "call runs in a fresh subprocess, so batch related work into one call. "
        "Use read_file/write_file/list_files for filesystem access if needed. "
        "When you have a complete answer, return it concisely as your final response."
    ),
    "researcher": (
        "You are a research worker. Your job is to find and verify information. "
        "Use execute(code) with httpx or playwright to fetch web pages and APIs; "
        "use read_file when given local source material. Prefer primary sources. "
        "Cite URLs in your final answer. If you cannot find something, say so "
        "explicitly — do not guess. Return your findings concisely."
    ),
    "coder": (
        "You are a code worker. Your job is to write or modify code precisely. "
        "Read the existing code (read_file / list_files) before changing it. Make "
        "minimal, focused edits. Use execute(code) to run, test, and verify. When "
        "something fails, fix the underlying cause; do not paper over it. Return "
        "a short summary of what you changed and any test output."
    ),
    "writer": (
        "You are a writing worker. Your job is to produce final-quality prose. "
        "Read source material via read_file before drafting. Match the requested "
        "length, tone, and audience. You do NOT have execute() — no shell, no "
        "code. Save drafts via write_file when asked. Return the final text."
    ),
}


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
    spec = get_model_spec(model)
    llm = spec.build_llm()
    use_cache = spec.provider in ("bedrock", "anthropic")

    # Safety wrappers — judge runs on the same model as the agent for now.
    # Override at the call site once a config knob is wired up.
    safe_execute = make_safe_execute(model)
    safe_write_file = make_safe_write_file(model)
    safe_write_artifact = make_safe_write_artifact(model)

    # ── Worker pool — role-typed, bound to THIS agent's model ────────────────
    # spawn_workers is built per agent (not a process-global registry) so a
    # conversation's workers always run on the same model as its main agent.

    _ROLE_TOOLS: dict[str, list] = {
        "general":    [safe_execute, read_file, safe_write_file, list_files, safe_write_artifact, read_artifact, artifact_list, search_documents, read_document],
        "researcher": [safe_execute, read_file, read_artifact, artifact_list, search_documents, read_document],
        "coder":      [safe_execute, read_file, safe_write_file, list_files],
        "writer":     [read_file, safe_write_file, safe_write_artifact, read_artifact, artifact_list, search_documents, read_document],
    }

    def _make_role_factory(role: str):
        prompt = _ROLE_PROMPTS[role]
        tools = _ROLE_TOOLS[role]
        role_llm = _with_llm_retry(llm.bind_tools(tools))

        def factory():
            async def role_model(state: AgentState, config: RunnableConfig) -> dict:
                # Strip historical thinking blocks (signatures don't survive
                # checkpoint round-trips → Bedrock rejects with "thinking.
                # signature: Field required"), and route through
                # build_llm_messages so any embedded SystemMessages are
                # collapsed into the single system prompt.
                history = strip_historical_thinking(list(state.get("messages", [])))
                history = repair_orphan_tool_calls(history)
                response = await role_llm.ainvoke(
                    build_llm_messages(prompt, use_cache, history),
                    config=config,
                )
                return {"messages": [response]}

            g = StateGraph(AgentState)  # type: ignore[type-var]
            g.add_node("agent", role_model)
            g.add_node("tools", ToolNode(tools))
            g.add_edge(START, "agent")
            g.add_conditional_edges("agent", tools_condition)
            g.add_edge("tools", "agent")
            return g.compile(name=role)

        return factory

    spawn_workers = make_spawn_workers(
        {role: _make_role_factory(role) for role in _ROLE_PROMPTS}
    )

    main_tools = [
        safe_execute,
        read_file,
        safe_write_file,
        list_files,
        safe_write_artifact,
        read_artifact,
        artifact_list,
        write_todos,
        set_todo_status,
        search_documents,
        read_document,
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

    # Bind tools so the LLM knows their schemas and emits structured tool_calls.
    # Without this, models hallucinate function-call syntax and fail validation
    # (Gemma's MALFORMED_FUNCTION_CALL, Claude's invalid_tool_calls, etc.).
    # The summarizer uses the raw `llm` since it doesn't tool-call.
    llm_with_tools = _with_llm_retry(llm.bind_tools(main_tools))
    llm_for_summary = _with_llm_retry(llm)

    # ── Graph nodes (closures capture llm, store, use_cache) ─────────────────

    async def model_request_node(state: AgentState, config: RunnableConfig) -> dict:
        """Call the LLM with the current system message (memory + todos injected fresh).

        Summarization is folded in here (was its own node) so each LLM round-trip
        costs 2 graph steps (model + tools) instead of 3. With recursion_limit=100
        the agent gets ~50 useful round-trips, which is plenty for code-first work.
        """
        if store is not None:
            memory = await _load_memory_from_store(store)
        else:
            memory = _load_memory_from_disk()
        system = _SYSTEM_PROMPT
        if memory:
            system = f"{system}\n\n## Agent Memory\n\n{memory}"
        todos = _normalise_todos(state.get("todos"))
        if todos:
            glyph = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
            todo_lines = "\n".join(f"{glyph[t['status']]} {t['text']}" for t in todos)
            system = f"{system}\n\n## Current Tasks\n\n{todo_lines}"
        raw_messages = list(state.get("messages", []))
        summarized = await maybe_summarize(raw_messages, llm=llm, summarizer=llm_for_summary)
        if summarized is not None:
            messages_for_llm, state_update_msgs = summarized
        else:
            messages_for_llm, state_update_msgs = raw_messages, []
        messages_for_llm = strip_historical_thinking(messages_for_llm)
        messages_for_llm = repair_orphan_tool_calls(messages_for_llm)
        response = await llm_with_tools.ainvoke(
            build_llm_messages(system, use_cache, messages_for_llm),
            config=config,
        )
        return {"messages": state_update_msgs + [response]}

    # ── Build graph ───────────────────────────────────────────────────────────

    graph = StateGraph(AgentState)  # type: ignore[type-var]
    graph.add_node("model_request", model_request_node)
    graph.add_node("tools", ToolNode(main_tools))

    graph.add_edge(START, "model_request")
    graph.add_conditional_edges("model_request", tools_condition)
    graph.add_edge("tools", "model_request")

    compiled = graph.compile(checkpointer=checkpointer, store=store, name="main")

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
