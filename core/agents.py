"""Agent builder."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from langchain_core.messages import AnyMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.utils.function_calling import convert_to_openai_tool

from .agent_loop import Agent, Run
from .config import get_config
from .compaction import apply_per_call_compaction, compact_threshold, maybe_compact
from .context_cache import CacheSegment, resolve_cache_ttl
from .mcp import get_mcp_server_summaries, get_mcp_tools_sync
from .tool_gate_node import make_tool_gate, tool_key_for
from .tool_policy import is_enabled
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
    honors_cache_control,
    is_valid_model,
    resolve_model_spec,
)

# Fallback for the no-runner path (CLI, tests). Mirrors
# RunnerConfig.cache_enabled_providers; honors_cache_control() narrows
# openrouter to the upstreams that actually honor cache_control blocks.
_DEFAULT_CACHE_PROVIDERS = frozenset({"bedrock", "anthropic", "openrouter"})
from .schemas import _normalise_todos
from core.doc_index import embeddings_available
from core.memory_store import load_core, search_memory
from core.skill_store import skill_catalog
from tools.artifacts import (
    list_artifacts as artifact_list,
    read_artifact,
    write_artifact,
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
    "skills": 4,                # full catalog only — a ranked shortlist is cacheable=False
    "mcp_servers": 5,           # lazy MCP catalog; changes only on config edit
    "browser": 6,               # environment fact; flips only when a browser starts/stops
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


# ── System prompt ────────────────────────────────────────────────────────────
# The prompt body lives in core/system_prompt.md (kept out of code so it can be
# edited without touching Python). Loaded once at import.

_SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text(encoding="utf-8").strip()

# ── Worker-role prompts ───────────────────────────────────────────────────────
# Each role gets a tuned prompt and (inside _build_agent) a tool subset.

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
    AGENTS.md blob. Trivial queries (greetings) skip fact retrieval only.

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
    # The how-to and core items are emitted on trivial turns too: they sit in
    # the cached prefix ahead of the conversation, so a "thanks" that dropped
    # them would rewrite that prefix and re-bill the whole history twice — once
    # dropping them, once restoring them on the next real question.
    if query and not is_trivial:
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
    to the latest user turn when the catalog is large. The full list is the
    same every turn and is cached; a narrowed list re-ranks per user turn, and
    anything cached ahead of the conversation that changes per turn re-bills
    the whole history, so that one goes to the uncached tail instead. The body
    stays out; the agent pulls it with `use_skill(name)`. Returns [] when there
    are no skills, so nothing about skills appears in the prompt until at least
    one exists.
    """
    try:
        catalog, ranked = await skill_catalog(query)
    except Exception as exc:
        logger.warning("skill catalog retrieval failed: %s", exc)
        return []
    if not catalog:
        return []
    lines = "\n".join(f"- **{c['name']}** — {c['description']}" for c in catalog)
    return [
        CacheSegment(
            name="skills",
            cacheable=not ranked,
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


_MCP_NAMES_SHOWN = 12


def _mcp_volatile_parts() -> list[CacheSegment]:
    """Advertise `lazy` MCP servers without paying for their tool schemas.

    A lazy server is unbound, so nothing in the prompt would otherwise reveal
    that it exists — and the agent cannot ask for a capability it has never
    heard of. Same trade as skills: names and counts are cheap, the schemas
    stay behind a `jarvis.mcp_help` call. Returns [] when every server is
    `always` (their tools are bound and self-describing).
    """
    try:
        summaries = get_mcp_server_summaries()
    except Exception as exc:
        logger.warning("MCP server summary failed: %s", exc)
        return []
    lazy = [s for s in summaries if s["load_mode"] == "lazy" and s["tool_count"]]
    if not lazy:
        return []
    lines = []
    for s in lazy:
        names = s["tools"][:_MCP_NAMES_SHOWN]
        extra = s["tool_count"] - len(names)
        listed = ", ".join(names) + (f", +{extra} more" if extra > 0 else "")
        lines.append(f"- **{s['name']}** ({s['tool_count']} tools): {listed}")
    return [
        CacheSegment(
            name="mcp_servers",
            content=(
                "## MCP Servers (on demand)\n\n"
                "External tool servers that are connected but NOT loaded as tools. "
                "Reach them from run_cell:\n"
                '`jarvis.mcp_help("<server>", "<tool>")` for the argument schema, then '
                '`jarvis.mcp_call("<server>", "<tool>", {...})` to run it. '
                "Check the schema before the first call to a tool — don't guess argument "
                "names.\n\n"
                + "\n".join(lines)
            ),
        )
    ]


# Probing CDP is a sub-millisecond local request, but `browser.cdp_url` may be
# a remote host with a one-second timeout, and this runs inside the per-turn
# retrieval path. Cached so a run of many turns pays it at most once a minute.
_BROWSER_PROBE_TTL = 60.0
_browser_probe: tuple[float, bool] = (0.0, False)


def _browser_reachable() -> bool:
    global _browser_probe
    checked_at, was_up = _browser_probe
    now = time.monotonic()
    if now - checked_at < _BROWSER_PROBE_TTL:
        return was_up
    try:
        from tools.browser import _endpoint_live, cdp_url

        up = _endpoint_live(cdp_url())
    except Exception as exc:
        logger.debug("browser probe failed: %s", exc)
        up = False
    _browser_probe = (now, up)
    return up


def _browser_volatile_parts() -> list[CacheSegment]:
    """Tell the agent a real browser is attached, when one actually is.

    Same problem `_mcp_volatile_parts` solves, and the same answer: the
    capability is reachable but nothing in the prompt reveals it, and the agent
    cannot ask for what it has never heard of. `read()`'s signature alone does
    not say a real browser is attached or that it can be driven, so an
    interactive page is a dead end — and an agent improvising with Playwright
    writes `chromium.launch()`, standing up a fresh headless browser with no
    profile (exactly what sites block) while a logged-in one sits idle on the
    CDP port.

    Conditional rather than a line in the system prompt, because a prompt line
    is billed on every call of every run forever, including the majority that
    never touch the web. This costs nothing when no browser is up, and when it
    does appear it states a *fact* — one is running right now — which is a
    stronger instruction than "you could".
    """
    if not _browser_reachable():
        return []
    return [
        CacheSegment(
            name="browser",
            content=(
                "## Live browser\n\n"
                "A real Chromium with a persistent, logged-in profile is running and "
                "attached over CDP. It is the way past sites that block automation, and "
                "the only way to click, scroll, or fill a form.\n\n"
                "- `read(url, browser=True)` — one-shot read of a page through it.\n"
                "- Drive it from run_cell with the **async** API (this kernel runs an "
                "event loop, so the sync one raises):\n"
                "  ```python\n"
                "  from tools.browser import apage\n"
                "  async with apage() as tab:          # full Playwright async API\n"
                "      await tab.goto(url)\n"
                "      await tab.click(\"text=Next\")\n"
                "      html = await tab.content()\n"
                "  ```\n"
                "  The tab persists between cells and turns — reopen `apage()` and it is "
                "still where you left it, logged in. Never `chromium.launch()`: that "
                "starts a fresh headless browser with no profile, which is what gets "
                "blocked in the first place."
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
        f"This conversation is part of project '{proj.name}'; all its conversations share the instructions "
        "and memory below.\n\n"
        "**Project memory is a short shared summary, not a log.** Append only a fact that would make a "
        "future conversation in this project act *differently* — stack and versions, architecture "
        "decisions, project-specific conventions, key file paths, API contracts, goals/status. One line "
        "each, no narration. If in doubt, don't write: every entry is re-read on every turn of every "
        "conversation here. General user info and global preferences go to `remember`; current-task "
        "progress goes to todos.\n"
        '`jarvis.project_memory(action="append"|"write", content=...)` — `write` replaces the whole memory '
        "with a condensed version once it starts repeating itself.\n\n"
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
        # Plain, non-imperative — an empty-state nag here read as a standing
        # order to fill it, which is most of why memory accumulated noise.
        memory_body = "### Project Memory\n\n(empty)"
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


async def _episode_volatile_parts(conversation_id: str | None, query: str) -> list[CacheSegment]:
    """Compacted-away stretches of this conversation that bear on `query`.

    Uncached: which episodes match changes with every user turn. Empty for any
    conversation that has never compacted, which is nearly all of them.
    """
    if not conversation_id:
        return []
    from core.episodes import render_episodes, search_episodes

    try:
        episodes = await search_episodes(conversation_id, query)
    except Exception as exc:
        logger.warning("episode retrieval failed: %s", exc)
        return []
    if not episodes:
        return []
    return [
        CacheSegment(
            name="earlier_in_conversation",
            content=render_episodes(episodes),
            cacheable=False,
        )
    ]


async def _compute_retrieval(
    store, query: str, conversation_id: str | None = None
) -> list[CacheSegment]:
    """Memory, skills and episode sections, fetched concurrently (deduplicated via query cache)."""
    try:
        mem_parts, skill_parts, episode_parts = await asyncio.gather(
            _memory_volatile_parts(store, query),
            _skills_volatile_parts(query),
            _episode_volatile_parts(conversation_id, query),
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
        return (
            mem_parts + skill_parts + episode_parts
            + _mcp_volatile_parts() + _browser_volatile_parts()
        )
    except Exception as exc:
        # Never let a cached failed task poison every iteration of the turn —
        # degrade to no retrieved context, matching the per-part fallbacks.
        logger.warning("retrieval context failed: %s", exc)
        return []


def _get_retrieval_task(
    store, query: str, key: str, conversation_id: str | None = None
) -> "asyncio.Task[list[CacheSegment]]":
    task = _retrieval_cache.get(key)
    if task is not None:
        _retrieval_cache.move_to_end(key)
        return task
    task = asyncio.create_task(_compute_retrieval(store, query, conversation_id))
    _retrieval_cache[key] = task
    while len(_retrieval_cache) > _RETRIEVAL_CACHE_MAX:
        _retrieval_cache.popitem(last=False)
    return task


def prefetch_retrieval(store, query: str, key: str, conversation_id: str | None = None) -> None:
    """Kick off this turn's memory+skill retrieval without awaiting it.

    Called by triggers (chat_runtime) with the id they will stamp on the
    user's HumanMessage, so the work overlaps the input safety gate and the
    graph's first `_retrieved_volatile_parts` call finds it already in flight.
    """
    _get_retrieval_task(store, query, key, conversation_id)


async def _retrieved_volatile_parts(
    store, messages: list[AnyMessage], conversation_id: str | None = None
) -> list[CacheSegment]:
    """Memory, skills and episode sections as tagged cache segments, cached per user turn.

    The cache key is the user message id, which is unique to one conversation,
    so `conversation_id` never needs to be part of it.
    """
    key = None
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            key = m.id
            break
    query = _latest_user_text(messages)
    if key is None:
        return await _compute_retrieval(store, query, conversation_id)
    return list(await _get_retrieval_task(store, query, key, conversation_id))


# ── Mid-run message queue ────────────────────────────────────────────────────

async def _drain_queued_input(config: RunnableConfig) -> list[HumanMessage]:
    """Messages the user queued while this run was in flight, taken now.

    The main model step is the only chokepoint every main-agent LLM call
    passes, which makes it the one safe injection point. A tool batch sits
    *between* an AIMessage's tool_calls and their results, and a HumanMessage
    spliced in there is the orphan pairing Anthropic/Bedrock reject outright;
    arriving here instead, the queued text lands after the current tool batch's
    results and before the next model call.

    The delivered HumanMessage keeps the durable row's id, so it is both the
    retrieval-cache key the queueMessage resolver already warmed and idempotent
    (a replacement in place) if it is ever written twice.
    """
    task_id = (config.get("configurable") or {}).get("message_id")
    if not task_id:
        return []
    from core.state import _tasks, emit_event

    task_state = _tasks.get(task_id)
    if task_state is None or not task_state.pending_input:
        return []
    drained = task_state.drain_input()

    # Best-effort: the message is already being delivered to the model, so a
    # failed status flip must not take the turn down with it. The row is left
    # `queued` and the next run adopts it — a duplicate is recoverable, a lost
    # user message is not.
    try:
        from db.engine import async_session
        from db.ops import mark_messages_delivered

        async with async_session() as session:
            await mark_messages_delivered(session, [m.id for m in drained])
    except Exception as exc:
        logger.warning("queued message status flip failed: %s", exc)

    emit_event(
        task_state, "queued_consumed",
        message_ids=[m.id for m in drained],
    )
    logger.info("delivered %d queued message(s) to run %s", len(drained), task_id)
    return [HumanMessage(content=m.text, id=m.id) for m in drained]


# ── Agent builder ─────────────────────────────────────────────────────────────

def _allowed(tools: list) -> list:
    """Drop tools a human has switched off in Settings → Tools.

    Applied at build time rather than per call: the toolset is baked into the
    agent by `bind_tools`, so a disabled tool must not reach the model's schema
    list at all. `core/tool_policy.set_tool_policy` drops the built-agent
    cache on every write, which is what makes a toggle take effect on the next
    run instead of the next restart.
    """
    kept = []
    for tool in tools:
        name = getattr(tool, "name", "")
        if name and not is_enabled(tool_key_for(name)):
            logger.info("tool %s is disabled by policy — not binding it", name)
            continue
        kept.append(tool)
    return kept


def _schema_tokens(tools: list) -> int:
    """chars/4 estimate of the bound tool schemas, as the provider is sent them.

    Measured against Google's own count for the main toolset: 817 estimated vs
    919 real — close enough for an overhead that is subtracted, not billed.
    """
    total = 0
    for tool in tools:
        try:
            total += len(json.dumps(convert_to_openai_tool(tool)))
        except Exception:  # an exotic MCP schema — skip it rather than fail the build
            continue
    return total // 4


def _build_agent(model: str, checkpointer: Any, store: Any, board: bool = False) -> Agent:
    # Degrade rather than raise on a stale id: this is the chokepoint every run
    # kind reaches, and a conversation/automation/board row can outlive the
    # model it names. Callers with a session resolve through db.ops.resolve_model
    # first, which lands on the operator's default; this catches the rest.
    spec = resolve_model_spec(model)
    llm = spec.build_llm()
    # Use runner's cache config if available (Jarvis runner seam), else local fallback.
    # Runner is set by entrypoint lifespan; CLI/tests have no runner.
    try:
        from core.runner import get_runner_or_none

        runner = get_runner_or_none()
        if runner is not None:
            use_cache = runner.should_use_cache(model)
        else:
            use_cache = honors_cache_control(spec, _DEFAULT_CACHE_PROVIDERS)
    except Exception:
        use_cache = honors_cache_control(spec, _DEFAULT_CACHE_PROVIDERS)

    # ── Worker pool — role-typed, bound to THIS agent's model ────────────────
    # spawn_workers is built per agent (not a process-global registry) so a
    # conversation's workers always run on the same model as its main agent.

    # MCP tools — optional, loaded from env/file config
    try:
        _mcp_tools_for_workers = get_mcp_tools_sync()
    except Exception:
        _mcp_tools_for_workers = []

    _ROLE_TOOLS: dict[str, list] = {
        "general":    [run_cell, read_file, write_file, list_files, write_artifact, read_artifact, artifact_list, search_documents, read_document] + _mcp_tools_for_workers,
        "researcher": [run_cell, read_file, read_artifact, artifact_list, search_documents, read_document] + _mcp_tools_for_workers,
        "coder":      [run_cell, read_file, write_file, list_files],
        "writer":     [read_file, write_file, write_artifact, read_artifact, artifact_list, search_documents, read_document],
    }

    def _make_role_factory(role: str):
        prompt = _ROLE_PROMPTS[role]
        # Workers get the same policy as the main agent: a tool a human
        # switched off must not come back through a subagent, and a gated one
        # must still ask.
        tools = _allowed(_ROLE_TOOLS[role])
        role_llm = _with_llm_retry(llm.bind_tools(tools))

        async def role_model(run: Run) -> list[BaseMessage]:
            # Strip historical thinking blocks (signatures don't survive
            # round-trips → Bedrock rejects with "thinking.signature: Field
            # required"), and route through build_llm_messages so any embedded
            # SystemMessages are collapsed into the single system prompt.
            # Use new per-call compaction (elide + collapse old tool groups)
            history = apply_per_call_compaction(list(run.thread.messages))
            history = strip_historical_thinking(history)
            history = repair_orphan_tool_calls(history)
            response = await role_llm.ainvoke(
                build_llm_messages(
                    prompt, use_cache, history, cache_provider=spec.provider
                ),
                config=run.model_config(),
            )
            return [response]

        worker = Agent(role, role_model, tools, gate=make_tool_gate(tools))
        return lambda: worker

    spawn_workers = make_spawn_workers(
        {role: _make_role_factory(role) for role in _ROLE_PROMPTS}
    )

    # Only tools coupled to the agent LOOP stay bound. Everything else lives
    # in the kernel-preloaded `jarvis` SDK (tools/sdk.py), discovered on demand
    # via jarvis.help() — reads hit the DB directly, writes go through the
    # server's own GraphQL API so in-process side effects (scheduler
    # registration, board dispatch) still fire.
    #
    # What must stay here and why:
    #   run_cell                  the door into the kernel
    #   write_todos/set_todo_*    write the run's thread state; a separate
    #                             process cannot reach it
    #   complete_task/block_task  act on the CURRENT run's lifecycle (board runs only)
    #   spawn_workers/run_workflow  run agents on this LLM binding
    #   write_artifact            its live side-panel event is tied to this
    #                             run's stream writer
    #   remember                  no createMemory mutation exists to route to
    main_tools = [
        run_cell,
        write_artifact,
        write_todos,
        set_todo_status,
        spawn_workers,
        run_workflow,
    ]

    # complete_task/block_task act on ToolContext.board_task_id, which only a
    # board run sets — everywhere else they can do nothing but return
    # "only available while executing a board task". Binding them anyway cost
    # ~340 tokens of schema on every LLM call of every chat/automation/workflow
    # run, and none of it is cached on providers outside should_use_cache().
    if board:
        main_tools += [complete_task, block_task]

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

    main_tools = _allowed(main_tools)

    # Bind tools so the LLM knows their schemas and emits structured tool_calls.
    # Without this, models hallucinate function-call syntax and fail validation
    # (Gemma's MALFORMED_FUNCTION_CALL, Claude's invalid_tool_calls, etc.).
    # The summarizer uses the raw `llm` since it doesn't tool-call.
    llm_with_tools = _with_llm_retry(llm.bind_tools(main_tools))
    llm_for_summary = _with_llm_retry(llm)

    # Sized from this model's context window, not a flat number shared by a
    # catalog whose windows span two orders of magnitude. Resolved once here
    # rather than per iteration: the agent is built per model, so this cannot
    # change over its lifetime.
    compaction_threshold = compact_threshold(model)
    # Same reasoning: provider is fixed for this agent, and the env read
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
    # Compaction counts history from the previous call's reported usage, minus
    # this estimate of everything else in the request (see
    # history_tokens_from_usage). The bound schemas are fixed per agent, so
    # they're measured once. None opts out: Ollama's
    # prompt_eval_count leaves out a KV-cached prefix, so it would undercount.
    tool_schema_tokens = _schema_tokens(main_tools) if spec.provider != "ollama" else None

    # ── The model step (closure captures llm, store, use_cache) ──────────────

    async def model_request_node(run: Run) -> list[BaseMessage]:
        """Call the LLM with the current system message (memory + todos injected fresh).

        Summarization is folded in here (was its own step) so each LLM round-trip
        costs 2 loop steps (model + tools) instead of 3. With recursion_limit=100
        the agent gets ~50 useful round-trips, which is plenty for code-first work.

        Caching: memory+skills+project instructions are cached system blocks
        ahead of the history, which carries a rolling breakpoint; todos, project
        memory and retrieved memories (which change per turn or mid-turn) go in
        an uncached tail after it. See build_llm_messages.
        """
        _phase = _PhaseTimer()
        config = run.config
        raw_messages = list(run.thread.messages)
        # Mid-run queue: delivered before retrieval runs, so the queued text is
        # what the memory/skill lookup keys off — the user's newest intent, not
        # the one the turn started with.
        queued = await _drain_queued_input(config)
        if queued:
            raw_messages.extend(queued)
        # Independent of each other, so they overlap rather than stack: the
        # retrieval is a cached per-turn task (usually already resolved), while
        # the project read is a fresh DB round-trip on EVERY model iteration by
        # design (see _project_volatile_parts). Awaiting them in sequence put
        # that round-trip on the critical path of all ~50 iterations of a run.
        retrieved_segments, project_segments = await asyncio.gather(
            _retrieved_volatile_parts(
                store, raw_messages, (config.get("configurable") or {}).get("thread_id")
            ),
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

        todos = _normalise_todos(run.thread.todos)
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
        usage_overhead_tokens = None
        if tool_schema_tokens is not None:
            overhead_chars = (
                len(_SYSTEM_PROMPT)
                + sum(len(s.content) for s in cache_segments)
                + len(volatile_suffix)
            )
            usage_overhead_tokens = tool_schema_tokens + overhead_chars // 4
        compaction = await maybe_compact(
            raw_messages,
            llm=llm,
            summarizer=llm_for_summary,
            threshold=compaction_threshold,
            usage_overhead_tokens=usage_overhead_tokens,
        )
        messages_for_llm = compaction.messages
        state_update_msgs = compaction.state_update
        if compaction.compacted and compaction.episode:
            # Compaction already spent two LLM calls on this iteration; one more
            # embedding is noise next to them, and awaiting it keeps the episode
            # from racing the next turn's retrieval. Best-effort: losing an
            # episode only loses detail the running summary still outlines.
            try:
                from core.episodes import record_episode

                await record_episode(
                    (config.get("configurable") or {}).get("thread_id") or "",
                    compaction.episode,
                    compaction.evicted_ids,
                )
            except Exception as exc:
                logger.warning("episode recording failed: %s", exc)
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
            cache_provider=spec.provider,
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

        response = await llm_with_tools.ainvoke(llm_messages, config=run.model_config())
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
        return [*state_update_msgs, *queued, response]

    return Agent(
        "main", model_request_node, main_tools,
        gate=make_tool_gate(main_tools), checkpointer=checkpointer, store=store,
    )


_cache: dict[tuple, Agent] = {}


def invalidate_agent_cache() -> None:
    """Drop all built agents so the next build_agent rebuilds them.

    Needed when the bound toolset changes at runtime (MCP server reload) —
    tools are baked in via bind_tools, so cached agents keep the old set.
    In-flight runs keep their already-built agent; only new runs rebuild.
    """
    _cache.clear()


def _build_cached(model: str, checkpointer: Any, store: Any, board: bool = False) -> Agent:
    # `board` is part of the key because the bound toolset differs: a board run
    # gets complete_task/block_task, nothing else does. Two agents per model at
    # most, and only on installs that actually use the board.
    key = (model, id(checkpointer), id(store), board)
    if key not in _cache:
        _cache[key] = _build_agent(model, checkpointer, store, board=board)
    return _cache[key]


def build_agent(
    model: str = DEFAULT_MODEL, checkpointer: Any = None, store: Any = None,
    invocation_context: Any = None, board: bool = False,
) -> Agent:
    """The agent for `model`, built once and shared by every run on it.

    ``checkpointer`` is LangGraph's saver, read only to convert a thread that
    has no transcript rows yet (see ``DbThread.load``). ``store`` reaches the
    tools as ``ToolContext.store``. Set ``board=True`` for a task-board run so
    the board lifecycle tools (complete_task/block_task) are bound; they are
    inert anywhere else.
    """
    return _build_cached(model, checkpointer, store, board=board)
