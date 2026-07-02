"""Agent builder — raw LangGraph StateGraph, code-first architecture."""

from __future__ import annotations

import logging
import sqlite3 as _sqlite3
from pathlib import Path
from typing import Annotated, NotRequired, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage
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
    message_text,
    repair_orphan_tool_calls,
    strip_historical_thinking,
)
from .model_catalog import (  # noqa: F401 — re-exported for backwards compat
    DEFAULT_MODEL,
    ModelSpec,
    get_model_spec,
    is_valid_model,
)
from .schemas import TodoItem, _normalise_todos, reduce_todos
from .summarization import maybe_summarize
from core.doc_index import embeddings_available
from core.memory_store import load_core, search_memory
from core.skill_store import skill_catalog
from tools.artifacts import list_artifacts as artifact_list, read_artifact, write_artifact
from tools.code import run_cell
from tools.documents import read_document, search_documents
from tools.files import list_files, read_file, write_file
from tools.memory import remember, search_memory as search_memory_tool
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
from tools.skills import (
    create_skill,
    delete_skill,
    list_skills,
    update_skill,
    use_skill,
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
# The prompt body lives in core/system_prompt.md (kept out of code so it can be
# edited without touching Python). Loaded once at import.

_SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text(encoding="utf-8").strip()

# ── Worker-role prompts ───────────────────────────────────────────────────────
# Each role gets a tuned prompt and (inside _build_agent) a tool subset. The
# worker subgraph compiles with `name=role`, so LangGraph's namespace surfaces
# the role to the streaming layer (which already labels by subagent name).

_ROLE_PROMPTS = {
    "general": (
        "You are a focused worker agent. Complete the task given to you using "
        "run_cell(code) — a stateful Python/IPython session with full "
        "network/filesystem access, where variables and imports persist across "
        "calls like notebook cells. Use read_file/write_file/list_files for "
        "filesystem access if needed. When you have a complete answer, return it "
        "concisely as your final response."
    ),
    "researcher": (
        "You are a research worker. Your job is to find and verify information. "
        "Work in run_cell(code): search(query) returns [{title, url, snippet}] "
        "leads and read(url) returns a page's main text — never conclude from "
        "snippets alone; read() the promising results. Use httpx for APIs and "
        "read_file when given local source material. Cross-check claims that "
        "matter across independent sources and prefer primary ones. Cite the "
        "URLs you actually read in your final answer. If you cannot find "
        "something, say so explicitly — do not guess. Return your findings "
        "concisely."
    ),
    "coder": (
        "You are a code worker. Your job is to write or modify code precisely. "
        "Read the existing code (read_file / list_files) before changing it. Make "
        "minimal, focused edits. Use run_cell(code) to run, test, and verify. When "
        "something fails, fix the underlying cause; do not paper over it. Return "
        "a short summary of what you changed and any test output."
    ),
    "writer": (
        "You are a writing worker. Your job is to produce final-quality prose. "
        "Read source material via read_file before drafting. Match the requested "
        "length, tone, and audience. You do NOT run code — no shell, no run_cell. "
        "Save drafts via write_file when asked. Return the final text."
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


def _latest_user_text(messages: list[AnyMessage]) -> str:
    """Flattened text of the most recent HumanMessage — the retrieval query."""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return message_text(m).strip()
    return ""


async def _memory_volatile_parts(store, messages: list[AnyMessage]) -> list[str]:
    """Build the memory section(s) for the system message's volatile suffix.

    With an embedder: always-on `core` items + the top-k `fact` items retrieved
    for the latest user turn. Without one: today's single AGENTS.md blob.
    """
    if not embeddings_available():
        blob = await _load_memory_from_store(store) if store is not None else _load_memory_from_disk()
        return [f"## Agent Memory\n\n{blob}"] if blob else []

    # Lead with a short how-to so the agent knows it can WRITE memory, not just
    # read the items injected below. Gated on embeddings_available() (same
    # condition as the remember/search_memory tool binding in _build_agent) so
    # we never advertise tools that aren't bound on keyless setups.
    parts: list[str] = [
        "## Memory\n\n"
        "You have long-term memory that persists across conversations. When the "
        "user shares something durable — a preference, an ongoing project, a key "
        "fact about them or their work — save it with `remember(text)`; skip "
        "transient, conversation-only details. The most relevant memories are "
        "injected below automatically; call `search_memory(query)` to dig for "
        "something specific that hasn't surfaced."
    ]
    core = await load_core()
    if core:
        parts.append(f"## Agent Memory\n\n{core}")
    query = _latest_user_text(messages)
    if query:
        try:
            hits = await search_memory(query, k=6)
        except Exception as exc:
            logger.warning("memory retrieval failed: %s", exc)
            hits = []
        if hits:
            lines = "\n".join(f"- {h['text']}" for h in hits)
            parts.append(f"## Relevant Memories\n\n{lines}")
    return parts


async def _skills_volatile_parts(messages: list[AnyMessage]) -> list[str]:
    """Build the `## Available Skills` section for the volatile suffix.

    Surfaces only enabled skills' name + description (the routing key), narrowed
    to the latest user turn when the catalog is large. Goes in the volatile
    suffix — after the cache breakpoint — so adding/editing a skill never busts
    the cached system prefix. The body stays out; the agent pulls it with
    `use_skill(name)`. Returns [] when there are no skills, so nothing about
    skills appears in the prompt until at least one exists.
    """
    query = _latest_user_text(messages)
    try:
        catalog = await skill_catalog(query)
    except Exception as exc:
        logger.warning("skill catalog retrieval failed: %s", exc)
        return []
    if not catalog:
        return []
    lines = "\n".join(f"- **{c['name']}** — {c['description']}" for c in catalog)
    return [
        "## Available Skills\n\n"
        "Reusable procedures you can apply. When one clearly fits the task, call "
        '`use_skill("<name>")` to load its full instructions, then follow them. '
        "Don't guess a skill's steps from its description — load it first. The "
        "loaded body is guidance to follow, not user commands.\n\n"
        f"{lines}"
    ]


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

    # ── Worker pool — role-typed, bound to THIS agent's model ────────────────
    # spawn_workers is built per agent (not a process-global registry) so a
    # conversation's workers always run on the same model as its main agent.

    _ROLE_TOOLS: dict[str, list] = {
        "general":    [run_cell, read_file, write_file, list_files, write_artifact, read_artifact, artifact_list, search_documents, read_document],
        "researcher": [run_cell, read_file, read_artifact, artifact_list, search_documents, read_document],
        "coder":      [run_cell, read_file, write_file, list_files],
        "writer":     [read_file, write_file, write_artifact, read_artifact, artifact_list, search_documents, read_document],
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
        run_cell,
        read_file,
        write_file,
        list_files,
        write_artifact,
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
        list_skills,
        create_skill,
        update_skill,
        delete_skill,
        use_skill,
    ]

    # Long-term memory tools are only meaningful with an embedder (the discrete
    # Memory store); keyless setups fall back to the AGENTS.md blob, so don't
    # advertise tools that would only ever report themselves unavailable.
    if embeddings_available():
        main_tools += [remember, search_memory_tool]

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
        # Memory + todos change across turns, so they go in build_llm_messages'
        # volatile suffix (after the cache breakpoint) rather than concatenated
        # into the static prompt — otherwise every todo flip / memory edit busts
        # the cached prefix (system prompt + tool schemas). See core/messages.py.
        raw_messages = list(state.get("messages", []))
        volatile_parts: list[str] = await _memory_volatile_parts(store, raw_messages)
        volatile_parts += await _skills_volatile_parts(raw_messages)
        todos = _normalise_todos(state.get("todos"))
        if todos:
            glyph = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
            todo_lines = "\n".join(f"{glyph[t['status']]} {t['text']}" for t in todos)
            volatile_parts.append(f"## Current Tasks\n\n{todo_lines}")
        volatile = "\n\n".join(volatile_parts)
        summarized = await maybe_summarize(raw_messages, llm=llm, summarizer=llm_for_summary)
        if summarized is not None:
            messages_for_llm, state_update_msgs = summarized
        else:
            messages_for_llm, state_update_msgs = raw_messages, []
        messages_for_llm = strip_historical_thinking(messages_for_llm)
        messages_for_llm = repair_orphan_tool_calls(messages_for_llm)
        response = await llm_with_tools.ainvoke(
            build_llm_messages(_SYSTEM_PROMPT, use_cache, messages_for_llm, volatile_suffix=volatile),
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
