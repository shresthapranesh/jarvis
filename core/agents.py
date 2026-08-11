"""Agent builder."""

from __future__ import annotations

import asyncio
import logging
import sqlite3 as _sqlite3
import time
from collections import OrderedDict
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
from .compaction import apply_per_call_compaction, compact_threshold, maybe_compact
from .context_cache import CacheSegment, resolve_cache_ttl
from .mcp import get_mcp_tools_sync
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
from core.doc_index import embeddings_available
from core.memory_store import load_core, search_memory
from core.skill_store import skill_catalog
from tools.artifacts import (
    list_artifacts as artifact_list,
    read_artifact,
    write_artifact,
    write_artifact_file,
)
from tools.code import run_cell
from tools.documents import read_document, search_documents
from tools.files import list_files, read_file, write_file
from tools.memory import remember
from tools.todos import set_todo_status, write_todos
from tools.workers import make_spawn_workers
from tools.board import block_task, complete_task
from tools.workflows import run_workflow

logger = logging.getLogger(__name__)


# ── Cache-segment stability ──────────────────────────────────────────────────
# Rank for ordering cacheable system-prompt segments, lowest = most stable.
# Prefix caching means a changed block invalidates every block after it, so the
# content that survives longest has to come first. Segments carry these names
# from the producers that build them (_memory_/_skills_/_project_volatile_parts);
# anything uncached skips this entirely and lands in the volatile suffix.
_SEGMENT_STABILITY: dict[str, int] = {
    "memory_howto": 0,          # static prose, never changes
    "core_memory": 1,           # always-on memory items; changes only on write
    "project_header": 2,        # project identity + standing instructions prose
    "project_instructions": 3,  # user-owned, edited rarely
    "skills": 4,                # re-ranked per turn, stable within one turn
}
_SEGMENT_STABILITY_DEFAULT = 50  # unknown names sort after all known ones


# ── Phase timing ─────────────────────────────────────────────────────────────

class _PhaseTimer:
    """Lap timer for attributing `model_request_node` wall-clock to phases.

    Every optimization in this node is a guess until the split between
    retrieval, compaction, prompt assembly, and the LLM round-trip is measured,
    so the laps run unconditionally — a `perf_counter` call per phase is far
    below the noise floor of what it measures. Only the log line is gated.
    """

    __slots__ = ("_last",)

    def __init__(self) -> None:
        self._last = time.perf_counter()

    def lap(self) -> float:
        """Milliseconds since the previous lap (or construction)."""
        now = time.perf_counter()
        delta = now - self._last
        self._last = now
        return delta * 1000.0


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
    # No google.api_core branch: langchain-google-genai 4.x talks to the
    # google-genai SDK, whose transient failures surface as
    # google.genai.errors.ServerError (caught above). The old client's
    # api_core exceptions are unreachable from every provider we build, and
    # google-api-core is not in the dependency tree at all — it used to arrive
    # transitively via browser-use, so the import silently succeeded and made
    # the branch look live.
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


async def _memory_volatile_parts(store, query: str) -> list[CacheSegment]:
    """Build the memory section(s) for the system message.

    With an embedder: always-on `core` items + the top-k `fact` items retrieved
    for `query` (the latest user turn's text). Without one: today's single
    AGENTS.md blob. Trivial queries (greetings) skip fact retrieval and, if core
    is large, skip the instructional header to save tokens.

    Each section is tagged with its own cacheability rather than left for the
    caller to infer from its heading text — `## Relevant Memories` re-ranks every
    turn, so caching it would bust the prefix on each request.
    """
    if not embeddings_available():
        blob = await _load_memory_from_store(store) if store is not None else _load_memory_from_disk()
        return [CacheSegment(name="core_memory", content=f"## Agent Memory\n\n{blob}")] if blob else []

    # Trivial detection — reuse same heuristic as query cache
    try:
        from core.doc_index import _is_trivial_query

        is_trivial = _is_trivial_query(query) if query else False
    except Exception:
        is_trivial = False

    core = await load_core()

    # For trivial greetings (hi, thanks), don't inject fact memories and
    # skip the instructional header if core is empty — saves ~100 tokens and
    # 2 embedding calls (already saved via cache, but also token cost)
    if is_trivial:
        if core:
            # Only core identity, no header, no relevant search
            return [CacheSegment(name="core_memory", content=f"## Agent Memory\n\n{core}")]
        return []

    # Lead with a short how-to so the agent knows it can WRITE memory, not just
    # read the items injected below. Gated on embeddings_available() (same
    # condition as the remember tool binding in _build_agent) so we never
    # advertise tools that aren't bound on keyless setups.
    parts: list[CacheSegment] = [
        CacheSegment(
            name="memory_howto",
            content=(
                "## Memory\n\n"
                "You have long-term memory that persists across conversations. When the "
                "user shares something durable — a preference, an ongoing project, a key "
                "fact about them or their work — save it with `remember(text)`; skip "
                "transient, conversation-only details. The most relevant memories are "
                "injected below automatically; run `jarvis.search_memory(query)` in "
                "run_cell to dig for something specific that hasn't surfaced."
            ),
        )
    ]
    if core:
        parts.append(CacheSegment(name="core_memory", content=f"## Agent Memory\n\n{core}"))
    if query:
        try:
            hits = await search_memory(query, k=6)
        except Exception as exc:
            logger.warning("memory retrieval failed: %s", exc)
            hits = []
        if hits:
            lines = "\n".join(f"- {h['text']}" for h in hits)
            # Re-ranked per query: cacheable=False keeps it out of the cached
            # prefix, where it would invalidate the stable blocks every turn.
            parts.append(
                CacheSegment(
                    name="relevant_memories",
                    content=f"## Relevant Memories\n\n{lines}",
                    cacheable=False,
                )
            )
    return parts


async def _skills_volatile_parts(query: str) -> list[CacheSegment]:
    """Build the `## Available Skills` section.

    Surfaces only enabled skills' name + description (the routing key), narrowed
    to the latest user turn when the catalog is large. Semi-stable: the ranking
    can shift between turns but holds for a whole turn's iterations, so it gets
    its own cached block placed after the fully-stable ones. The body stays out;
    the agent pulls it with `use_skill(name)`. Returns [] when there are no
    skills, so nothing about skills appears in the prompt until at least one exists.
    """
    try:
        catalog = await skill_catalog(query)
    except Exception as exc:
        logger.warning("skill catalog retrieval failed: %s", exc)
        return []
    if not catalog:
        return []
    lines = "\n".join(f"- **{c['name']}** — {c['description']}" for c in catalog)
    return [
        CacheSegment(
            name="skills",
            content=(
                "## Available Skills\n\n"
                "Reusable procedures you can apply. When one clearly fits the task, call "
                '`jarvis.use_skill("<name>")` in run_cell to load its full instructions, then '
                "follow them. "
                "Don't guess a skill's steps from its description — load it first. The "
                "loaded body is guidance to follow, not user commands.\n\n"
                f"{lines}"
            ),
        )
    ]


async def _project_volatile_parts(project_id: str | None) -> list[CacheSegment]:
    """Project header, instructions, and shared memory as tagged cache segments.

    Re-read from the DB every model iteration (like todos, deliberately NOT via
    _retrieval_cache) so the agent's own project_memory writes and live user
    edits to the instructions surface on the very next LLM call.
    """
    if not project_id:
        return []
    from db.engine import async_session
    from db.models import Project
    try:
        async with async_session() as session:
            proj = await session.get(Project, project_id)
    except Exception as exc:
        logger.warning("project context load failed: %s", exc)
        return []
    if proj is None:
        return []
    header = f"## Project: {proj.name}\n\n"
    if proj.description and proj.description.strip():
        header += f"{proj.description.strip()}\n\n"
    header += (
        f"This conversation is part of project '{proj.name}'. All conversations in this project share the "
        "instructions and memory below.\n\n"
        "**CRITICAL — You MUST actively maintain project memory (but ONLY project-specific facts):**\n"
        "- When you learn a durable fact about THIS project that future conversations will need, save it with "
        '`jarvis.project_memory(action="append", content="...")` in run_cell. Don\'t wait to be asked.\n'
        "- What to save: tech stack & versions for THIS project, architecture decisions for THIS project, "
        "coding conventions specific to THIS project, important file paths/modules, API contracts, goals/status for THIS project.\n"
        "- What NOT to save: general user info (name, role, background), general communication prefs "
        '("likes concise answers"), global coding prefs that apply to ALL projects — those belong to the `remember` tool, not project memory. '
        "If a preference is not explicitly tied to THIS project, use `remember` instead.\n"
        "- If project memory is empty and this conversation established project-specific stack/decisions/files, initialize it.\n"
        "- Before finishing a task, ask: did we learn something project-specific that future chats in THIS project need? If yes, update.\n"
        "- If existing memory is outdated/conflicting, use `jarvis.project_memory(action=\"write\", content=...)` to replace with condensed version.\n"
        "- Current memory appears below under '### Project Memory' (if empty, placeholder shows — only init if you have project-specific facts).\n\n"
        "**Earlier conversations in this project are searchable.** Project memory is a summary, not a "
        "transcript — when the user refers to something decided or discussed before, or you need the detail "
        "behind a memory entry, run `jarvis.search_conversations(\"<the exact terms you expect>\")` in run_cell "
        "and follow a hit with `jarvis.read_conversation(conversation_id)`. It is keyword search, so use the "
        "concrete names/ids/filenames, not a paraphrase. Search before saying you have no record of something."
    )
    parts = [CacheSegment(name="project_header", content=header)]
    if proj.instructions.strip():
        parts.append(
            CacheSegment(
                name="project_instructions",
                content=f"### Project Instructions\n\n{proj.instructions.strip()}",
            )
        )
    # Live-edited by the agent's own project_memory tool mid-turn, so it must
    # stay out of the cached prefix to surface on the very next LLM call.
    if proj.memory.strip():
        memory_body = f"### Project Memory\n\n{proj.memory.strip()}"
    else:
        memory_body = (
            "### Project Memory\n\n(empty — initialize with `jarvis.project_memory(action=\"append\", content=...)` "
            "when you learn durable facts like stack, decisions, conventions, or goals)"
        )
    parts.append(CacheSegment(name="project_memory", content=memory_body, cacheable=False))
    return parts


# Retrieval-backed context (memory + skills) is computed once per user turn
# and reused across that turn's agent-loop iterations: the retrieval query is
# the latest HumanMessage, which doesn't change mid-turn, so recomputing every
# iteration burns embedding calls on identical results. Keyed by the latest
# human message's id (unique per turn — add_messages assigns UUIDs). Items
# written mid-turn (remember / manage_skills) surface on the next user turn.
#
# The cache holds asyncio.Tasks rather than values so a trigger can *prefetch*
# the retrieval (overlapping the embedding round-trips with the input safety
# gate — see prefetch_retrieval) and the graph's first iteration awaits the
# same in-flight task instead of racing it with a duplicate computation.
_RETRIEVAL_CACHE_MAX = 256
_retrieval_cache: "OrderedDict[str, asyncio.Task[list[CacheSegment]]]" = OrderedDict()


async def _compute_retrieval(store, query: str) -> list[CacheSegment]:
    """Memory + skills sections, fetched concurrently (deduplicated via query cache)."""
    try:
        mem_parts, skill_parts = await asyncio.gather(
            _memory_volatile_parts(store, query),
            _skills_volatile_parts(query),
        )
        # Emit cache stats for /server-logs observability (debug level per-turn,
        # info level periodically via doc_index itself)
        try:
            from core.doc_index import get_query_cache_stats

            stats = get_query_cache_stats()
            logger.debug(
                "retrieval done query_len=%d mem_parts=%d skill_parts=%d cache_hit_rate=%.1f%% saved=%d",
                len(query),
                len(mem_parts),
                len(skill_parts),
                stats["hit_rate"] * 100,
                stats["saved_calls"],
            )
        except Exception:
            pass
        return mem_parts + skill_parts
    except Exception as exc:
        # Never let a cached failed task poison every iteration of the turn —
        # degrade to no retrieved context, matching the per-part fallbacks.
        logger.warning("retrieval context failed: %s", exc)
        return []


def _get_retrieval_task(store, query: str, key: str) -> "asyncio.Task[list[CacheSegment]]":
    task = _retrieval_cache.get(key)
    if task is not None:
        _retrieval_cache.move_to_end(key)
        return task
    task = asyncio.create_task(_compute_retrieval(store, query))
    _retrieval_cache[key] = task
    while len(_retrieval_cache) > _RETRIEVAL_CACHE_MAX:
        _retrieval_cache.popitem(last=False)
    return task


def prefetch_retrieval(store, query: str, key: str) -> None:
    """Kick off this turn's memory+skill retrieval without awaiting it.

    Called by triggers (chat_runtime) with the id they will stamp on the
    user's HumanMessage, so the work overlaps the input safety gate and the
    graph's first `_retrieved_volatile_parts` call finds it already in flight.
    """
    _get_retrieval_task(store, query, key)


async def _retrieved_volatile_parts(store, messages: list[AnyMessage]) -> list[CacheSegment]:
    """Memory + skills sections as tagged cache segments, cached per user turn."""
    key = None
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            key = m.id
            break
    query = _latest_user_text(messages)
    if key is None:
        return await _compute_retrieval(store, query)
    return list(await _get_retrieval_task(store, query, key))


# ── Checkpointer ─────────────────────────────────────────────────────────────

_sync_checkpointer: SqliteSaver | None = None


def _get_sync_checkpointer() -> SqliteSaver:
    global _sync_checkpointer
    if _sync_checkpointer is None:
        conn = _sqlite3.connect(get_config().checkpoints_db, check_same_thread=False)
        _sync_checkpointer = SqliteSaver(conn=conn)
    return _sync_checkpointer


def close_sync_checkpointer() -> None:
    """Close the lazy sync checkpointer connection, if one was ever opened.

    Only the no-runner path (CLI, tests) builds it; the server uses the async
    saver from the lifespan. Safe to call unconditionally.
    """
    global _sync_checkpointer
    saver, _sync_checkpointer = _sync_checkpointer, None
    if saver is not None:
        try:
            saver.conn.close()
        except Exception:  # already closed / mid-teardown
            pass


# ── Agent builder ─────────────────────────────────────────────────────────────

def _build_agent(model: str, checkpointer, store: AsyncSqliteStore | None) -> CompiledStateGraph:
    spec = get_model_spec(model)
    llm = spec.build_llm()
    # Use runner's cache config if available (Jarvis runner seam), else local fallback.
    # Runner is set by entrypoint lifespan; CLI/tests have no runner.
    try:
        from core.runner import get_runner_or_none

        runner = get_runner_or_none()
        if runner is not None:
            use_cache = runner.should_use_cache(model)
        else:
            use_cache = spec.provider in ("bedrock", "anthropic")
    except Exception:
        use_cache = spec.provider in ("bedrock", "anthropic")

    # ── Worker pool — role-typed, bound to THIS agent's model ────────────────
    # spawn_workers is built per agent (not a process-global registry) so a
    # conversation's workers always run on the same model as its main agent.

    # MCP tools — optional, loaded from env/file config
    try:
        _mcp_tools_for_workers = get_mcp_tools_sync()
    except Exception:
        _mcp_tools_for_workers = []

    _ROLE_TOOLS: dict[str, list] = {
        "general":    [run_cell, read_file, write_file, list_files, write_artifact, write_artifact_file, read_artifact, artifact_list, search_documents, read_document] + _mcp_tools_for_workers,
        "researcher": [run_cell, read_file, read_artifact, artifact_list, search_documents, read_document] + _mcp_tools_for_workers,
        "coder":      [run_cell, read_file, write_file, list_files],
        "writer":     [read_file, write_file, write_artifact, write_artifact_file, read_artifact, artifact_list, search_documents, read_document],
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
                # Use new per-call compaction (elide + collapse old tool groups)
                history = apply_per_call_compaction(list(state.get("messages", [])))
                history = strip_historical_thinking(history)
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

    # Only tools coupled to the agent GRAPH stay bound. Everything else lives
    # in the kernel-preloaded `jarvis` SDK (tools/sdk.py), discovered on demand
    # via jarvis.help() — reads hit the DB directly, writes go through the
    # server's own GraphQL API so in-process side effects (scheduler
    # registration, board dispatch) still fire.
    #
    # What must stay here and why:
    #   run_cell                  the door into the kernel
    #   write_todos/set_todo_*    return Command(update=...) state deltas; a
    #                             separate process cannot write the reducer
    #   complete_task/block_task  act on the CURRENT run's lifecycle
    #   spawn_workers/run_workflow  instantiate subgraphs on this LLM binding
    #   write_artifact/_file      its live side-panel event is tied to this
    #                             run's stream writer
    #   remember                  no createMemory mutation exists to route to
    main_tools = [
        run_cell,
        write_artifact,
        write_artifact_file,
        write_todos,
        set_todo_status,
        spawn_workers,
        complete_task,
        block_task,
        run_workflow,
    ]

    # The memory write path is only meaningful with an embedder (the discrete
    # Memory store); keyless setups fall back to the AGENTS.md blob, so don't
    # advertise a tool that would only ever report itself unavailable.
    # (search_memory moved into the kernel SDK.)
    if embeddings_available():
        main_tools.append(remember)

    # MCP tools (MCP toolset) — loaded from env/file config via core/mcp.py.
    # Returns [] when no MCP servers configured, so agent works without MCP.
    if _mcp_tools_for_workers:
        main_tools += _mcp_tools_for_workers
        logger.info("Added %d MCP tools to main agent", len(_mcp_tools_for_workers))

    # Bind tools so the LLM knows their schemas and emits structured tool_calls.
    # Without this, models hallucinate function-call syntax and fail validation
    # (Gemma's MALFORMED_FUNCTION_CALL, Claude's invalid_tool_calls, etc.).
    # The summarizer uses the raw `llm` since it doesn't tool-call.
    llm_with_tools = _with_llm_retry(llm.bind_tools(main_tools))
    llm_for_summary = _with_llm_retry(llm)

    # Sized from this model's context window, not a flat number shared by a
    # catalog whose windows span two orders of magnitude. Resolved once here
    # rather than per iteration: the graph is built per model, so this cannot
    # change over the compiled agent's lifetime.
    compaction_threshold = compact_threshold(model)
    # Same reasoning: provider is fixed for this compiled graph, and the env read
    # + validation shouldn't repeat on every model iteration.
    cache_ttl = resolve_cache_ttl(spec.provider)
    logger.info(
        "agent %s: compaction threshold %d tokens (window=%s)",
        model,
        compaction_threshold,
        spec.context_window or "unknown",
    )
    if cache_ttl != "5m":
        logger.info("agent %s: cache_control ttl=%s", model, cache_ttl)

    # ── Graph nodes (closures capture llm, store, use_cache) ─────────────────

    async def model_request_node(state: AgentState, config: RunnableConfig) -> dict:
        """Call the LLM with the current system message (memory + todos injected fresh).

        Summarization is folded in here (was its own node) so each LLM round-trip
        costs 2 graph steps (model + tools) instead of 3. With recursion_limit=100
        the agent gets ~50 useful round-trips, which is plenty for code-first work.

        multi-breakpoint caching: memory+skills+project instructions are
        placed in separate cached blocks (up to 4 breakpoints), while todos and
        project memory (which can change mid-turn via tools) stay volatile.
        """
        _phase = _PhaseTimer()
        raw_messages = list(state.get("messages", []))
        # Independent of each other, so they overlap rather than stack: the
        # retrieval is a cached per-turn task (usually already resolved), while
        # the project read is a fresh DB round-trip on EVERY model iteration by
        # design (see _project_volatile_parts). Awaiting them in sequence put
        # that round-trip on the critical path of all ~50 iterations of a run.
        retrieved_segments, project_segments = await asyncio.gather(
            _retrieved_volatile_parts(store, raw_messages),
            _project_volatile_parts((config.get("configurable") or {}).get("project_id")),
        )
        t_context = _phase.lap()

        # ── Context-cache ordering ───────────────────────────────────────────
        # Prefix caching invalidates every block after a changed one, so cached
        # segments are emitted most-stable-first. Each producer tags its own
        # segments (name + cacheable); ordering here is a rank lookup rather than
        # the heading-sniffing this used to do, so renaming a section heading
        # can no longer silently move content across the cache breakpoint.
        segments = retrieved_segments + project_segments
        cache_segments = sorted(
            (s for s in segments if s.cacheable and s.content.strip()),
            key=lambda s: _SEGMENT_STABILITY.get(s.name, _SEGMENT_STABILITY_DEFAULT),
        )
        volatile_non_cached: list[str] = [
            s.content for s in segments if not s.cacheable and s.content.strip()
        ]

        todos = _normalise_todos(state.get("todos"))
        if todos:
            glyph = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
            todo_lines = "\n".join(f"{glyph[t['status']]} {t['text']}" for t in todos)
            volatile_non_cached.append(f"## Current Tasks\n\n{todo_lines}")
        else:
            # ── Planning mode injection (planning mode) ──────────────
            # If no todos yet and query looks complex, inject a strong directive
            # forcing the model to call write_todos first. This is the cheapest
            # planning path (no extra LLM call) and is gated by
            # JARVIS_PLANNING_MODE env (auto/always/off, default auto).
            try:
                from core.planning import build_planning_directive

                latest_query = _latest_user_text(raw_messages)
                directive = build_planning_directive(latest_query)
                if directive:
                    volatile_non_cached.append(directive)
            except Exception:
                pass

        volatile_suffix = "\n\n".join(volatile_non_cached)
        t_segments = _phase.lap()

        # ── New compaction pipeline (MAF + Jarvis inspired) ─────────────────
        # maybe_compact does elide-first token counting (per-call view) but
        # groups/removes against raw_messages, and hands back the leaned view it
        # already built — re-running apply_per_call_compaction here would repeat
        # the elide and grouping passes on every iteration. See core/compaction.py.
        compaction = await maybe_compact(
            raw_messages,
            llm=llm,
            summarizer=llm_for_summary,
            threshold=compaction_threshold,
        )
        messages_for_llm = compaction.messages
        state_update_msgs = compaction.state_update
        t_compaction = _phase.lap()

        messages_for_llm = strip_historical_thinking(messages_for_llm)
        messages_for_llm = repair_orphan_tool_calls(messages_for_llm)

        # Build LLM messages with multi-breakpoint cache (Jarvis)
        llm_messages = build_llm_messages(
            _SYSTEM_PROMPT,
            use_cache,
            messages_for_llm,
            volatile_suffix=volatile_suffix,
            cache_segments=cache_segments if cache_segments else None,
            cache_ttl=cache_ttl,
        )
        t_build = _phase.lap()

        # Log cache stats for observability
        try:
            from core.context_cache import get_last_cache_stats

            stats = get_last_cache_stats()
            if stats:
                logger.debug(
                    "cache built: cached=%d/%d bp=%d/%d cached_tokens~%d volatile~%d",
                    stats.segments_cached,
                    stats.segments_total,
                    stats.breakpoints_used,
                    4,
                    stats.cached_tokens_est,
                    stats.volatile_tokens_est,
                )
        except Exception:
            pass

        response = await llm_with_tools.ainvoke(llm_messages, config=config)
        t_llm = _phase.lap()

        # Phase attribution — the split between these is what tells you whether
        # a given optimization is worth making. Cheap to collect, so it always
        # runs; only the formatting is gated on the log level.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "model_request phases (ms): context=%.1f segments=%.1f "
                "compaction=%.1f build=%.1f llm=%.1f | msgs=%d->%d compacted=%s",
                t_context,
                t_segments,
                t_compaction,
                t_build,
                t_llm,
                len(raw_messages),
                len(messages_for_llm),
                compaction.compacted,
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


def invalidate_agent_cache() -> None:
    """Drop all compiled agents so the next build_agent rebuilds them.

    Needed when the bound toolset changes at runtime (MCP server reload) —
    tools are baked in via bind_tools, so cached graphs keep the old set.
    In-flight runs keep their already-built graph; only new runs rebuild.
    """
    _cache.clear()


def _build_cached(model: str, checkpointer, store) -> CompiledStateGraph:
    key = (model, id(checkpointer), id(store))
    if key not in _cache:
        _cache[key] = _build_agent(model, checkpointer, store)
    return _cache[key]


def build_agent(model: str = DEFAULT_MODEL, checkpointer=None, store: AsyncSqliteStore | None = None, invocation_context=None) -> CompiledStateGraph:
    """Build the agent. Defaults to sync SqliteSaver for CLI; server passes async variants."""
    if checkpointer is None:
        checkpointer = _get_sync_checkpointer()
    return _build_cached(model, checkpointer, store)
