"""Project memory consolidation — the scheduled writer behind `Project.memory`.

Project memory has three writers, and what keeps them from fighting is that
they hold **different authorities**, not different schedules:

| writer                          | latency  | may add | may delete |
|---------------------------------|----------|---------|------------|
| agent, in-band (`jarvis.project_memory`) | instant  | yes     | no         |
| this job, **merge** mode        | ~15-45m  | yes     | no         |
| this job, **rewrite** mode      | ~daily   | yes     | **yes**    |

Only one thing can shrink memory. Two tiers that could both evict would fight
over the same entries on different schedules, and nothing would be able to tell
which one dropped a fact.

Because all three derive from the same transcript, a lost update is
self-healing: if the agent appends at 10:59 and a merge pass clobbers it at
11:00, the next pass re-derives the fact from the messages it came from. The
compare-and-set on `Project.updated_at` is therefore a courtesy, not a
correctness requirement.

Watermarks live in the LangGraph store (mirroring memory_consolidation's
`last_run_at`), so none of this needs a schema migration.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.store.sqlite.aio import AsyncSqliteStore

from core.text_dedupe import dedupe_against
from db.engine import async_session
from db.models import Project
from db.ops import (
    get_default_model,
    get_project_activity_since,
    get_project_messages_since,
    list_projects,
    update_project,
)

logger = logging.getLogger(__name__)

_META_NS = ("project_memory_consolidation",)

_MEMORY_CAP = 24_000
_MAX_BULLETS = 20
_NO_UPDATE_MARKER = "__NO_UPDATE__"

# A conversation must be idle this long before its material is consolidated.
# This is the missing end-of-conversation signal: memory-worthy facts settle
# when a work session ends, not mid-turn, so waiting turns O(turns) LLM calls
# into O(sessions).
_QUIET_MINUTES = 15
# Even a project that never goes quiet gets a pass eventually.
_MAX_STALENESS_HOURS = 24
# Rewrite (the expensive, delete-capable mode) runs at most this often.
_REWRITE_INTERVAL_HOURS = 24
# Don't spend an LLM call on a trivial amount of new material.
_MIN_NEW_CHARS = 600

# One pass reads at most this much transcript. Anything past it stays behind the
# watermark and is picked up next tick.
_MATERIAL_BUDGET = 24_000
_MESSAGE_FETCH_LIMIT = 400
# Asymmetric on purpose: user messages are short and assistant messages are
# where decisions, contracts and rationale actually get stated. Truncating both
# at one limit guts the side that carries the substance, and biases extraction
# toward whatever is easy to say in the first few hundred characters.
_USER_MSG_CAP = 1_200
_ASSISTANT_MSG_CAP = 3_000
# Projects consolidated per tick — bounds the cost of one wakeup.
_MAX_PROJECTS_PER_TICK = 8


# Both prompts are deliberately frugal. What they emit is re-read on every turn
# of every conversation in the project, so the bar is "would a future
# conversation act differently for knowing this" — and emitting nothing is the
# expected outcome for most sessions, not a failure.

_MERGE_SYSTEM_PROMPT = f"""You extract durable facts for a project's shared memory: a compact summary that every conversation in this project re-reads on every turn.

You are given the project's existing memory (possibly empty) and a transcript of recent conversations from that project. Output ONLY the bullets that should be ADDED — never restate, reword, or reorganize what the existing memory already says.

Add a fact only if a future conversation in this project would act *differently* for knowing it, and only if the transcript ties it to THIS project: stack and versions, architecture decisions, project-specific conventions, key file paths/modules, API contracts, goals/status.

Never add: the user's personal info or background; communication preferences; coding preferences that aren't specific to this project; how a task went or what you did; general knowledge; small talk; secrets or tokens. Never invent anything absent from the transcript.

Output: markdown bullets, one line each, no preamble and no code fences. At most 5 new bullets — usually zero or one.

If the transcript adds nothing that clears the bar, output exactly: {_NO_UPDATE_MARKER}
That is the common case and a correct answer — do not pad."""

_REWRITE_SYSTEM_PROMPT = f"""You are rewriting a project's shared memory: a compact summary that every conversation in this project re-reads on every turn. This is the only pass allowed to remove things, so pruning is the job.

You are given the current memory and a transcript of recent conversations from the project. Return the complete replacement memory: keep what still matters, drop what is outdated, redundant, or too trivial to justify permanent context, prefer the transcript wherever it contradicts the memory, and merge in genuinely new project-specific facts.

Keep only: stack and versions, architecture decisions, project-specific conventions, key file paths/modules, API contracts, goals/status — all tied to THIS project. Remove personal info, general preferences, task-progress notes, small talk, and anything you would not bother telling a new teammate. Never invent beyond the transcript and the existing memory.

Output: markdown bullets, one line each, no preamble and no code fences. **Hard limit {_MAX_BULLETS} bullets** — if more than that survive, drop the least useful until you are under it.

If the memory is already correct and the transcript changes nothing, output exactly: {_NO_UPDATE_MARKER}"""


# ── helpers ───────────────────────────────────────────────────────────────────

def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything here compares in UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _coerce_text(raw) -> str:
    if isinstance(raw, list):
        return "".join(b.get("text", "") for b in raw if isinstance(b, dict)).strip()
    return str(raw).strip()


def _count_bullets(memory: str) -> int:
    return sum(1 for line in memory.splitlines() if line.strip().startswith(("-", "*", "•")))


def _render_material(messages: list[dict]) -> tuple[str, datetime | None]:
    """Transcript block under `_MATERIAL_BUDGET`, plus the last timestamp consumed.

    Returns the watermark the caller may advance to — never further than what
    actually made it into the prompt, so a budget cut becomes a backlog rather
    than a silent gap.
    """
    lines: list[str] = []
    total = 0
    consumed_through: datetime | None = None
    for m in messages:
        cap = _USER_MSG_CAP if m["role"] == "user" else _ASSISTANT_MSG_CAP
        body = (m["content"] or "").strip()
        if len(body) > cap:
            body = body[:cap] + " …[truncated]"
        stamp = _aware(m["created_at"])
        line = f"[{stamp:%Y-%m-%d %H:%M}] {m['title']} | {m['role'].upper()}: {body}"
        if total + len(line) > _MATERIAL_BUDGET and lines:
            break
        lines.append(line)
        total += len(line)
        consumed_through = stamp
    return "\n".join(lines), consumed_through


async def _load_meta(store: AsyncSqliteStore, project_id: str) -> tuple[datetime | None, datetime | None]:
    """(messages_through, last_rewrite_at) — the project's two watermarks."""
    item = await store.aget(_META_NS, project_id)
    if item is None:
        return None, None

    def _parse(key: str) -> datetime | None:
        raw = item.value.get(key)
        if not raw:
            return None
        try:
            return _aware(datetime.fromisoformat(raw))
        except (TypeError, ValueError):
            return None

    return _parse("messages_through"), _parse("last_rewrite_at")


async def _save_meta(
    store: AsyncSqliteStore,
    project_id: str,
    messages_through: datetime | None,
    last_rewrite_at: datetime | None,
) -> None:
    await store.aput(
        _META_NS,
        project_id,
        {
            "messages_through": messages_through.isoformat() if messages_through else None,
            "last_rewrite_at": last_rewrite_at.isoformat() if last_rewrite_at else None,
        },
    )


async def _resolve_llm(model_id: str | None):
    if model_id is None:
        async with async_session() as session:
            model_id = await get_default_model(session)
    from core.model_catalog import get_model_spec

    try:
        return get_model_spec(model_id).build_llm(), model_id
    except Exception as exc:
        logger.warning("project memory: cannot build LLM %s: %s", model_id, exc)
        return None, model_id


async def _ask(llm, system: str, user: str) -> str:
    response = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
    return _coerce_text(response.content)


async def _commit_memory(project_id: str, new_memory: str, seen_updated_at: datetime | None) -> bool:
    """Compare-and-set on `Project.updated_at`. False means someone else wrote."""
    async with async_session() as session:
        proj = await session.get(Project, project_id)
        if proj is None:
            return False
        if seen_updated_at is not None and _aware(proj.updated_at) != seen_updated_at:
            logger.info(
                "project memory: %s changed under us — skipping, next pass re-derives", project_id
            )
            return False
        await update_project(session, project_id, memory=new_memory)
    return True


# ── one project ───────────────────────────────────────────────────────────────

async def consolidate_project_memory(
    store: AsyncSqliteStore,
    project_id: str,
    model_id: str | None = None,
    force: bool = False,
) -> str:
    """Run one consolidation pass for a project. Returns a human-readable summary.

    `force` skips the quiet-period and minimum-material gates (the on-demand
    mutation), but not the "is there anything new at all" check.
    """
    now = datetime.now(timezone.utc)
    messages_through, last_rewrite_at = await _load_meta(store, project_id)

    async with async_session() as session:
        proj = await session.get(Project, project_id)
        if proj is None:
            return f"skipped: project {project_id} not found"
        existing = (proj.memory or "").strip()
        seen_updated_at = _aware(proj.updated_at)
        count, oldest, newest, chars = await get_project_activity_since(
            session, project_id, messages_through
        )

    if not count or newest is None:
        return "skipped: no new messages since last run"

    newest = _aware(newest)
    quiet_for = (now - newest).total_seconds() / 60 if newest else 0.0
    # Measured from the *oldest unconsumed message*, not from the watermark: a
    # project that has never been consolidated has no watermark, and treating
    # that as maximally stale would defeat the quiet gate on its very first pass.
    oldest = _aware(oldest)
    waiting_hours = (now - oldest).total_seconds() / 3600 if oldest else 0.0

    if not force:
        # A project still being worked in waits — unless material has been
        # queued so long that waiting for silence would mean never consolidating.
        if quiet_for < _QUIET_MINUTES and waiting_hours < _MAX_STALENESS_HOURS:
            return f"skipped: active {quiet_for:.0f}m ago"
        if chars < _MIN_NEW_CHARS:
            return f"skipped: only {chars} new chars"

    # Rewrite is the expensive, delete-capable mode — earn it. A project with no
    # recorded rewrite is NOT treated as due: the first pass over existing memory
    # should merge, and the clock starts when that pass records `last_rewrite_at`.
    at_cap = _count_bullets(existing) >= _MAX_BULLETS or len(existing) > _MEMORY_CAP * 0.8
    rewrite_due = last_rewrite_at is not None and (
        (now - last_rewrite_at).total_seconds() / 3600 >= _REWRITE_INTERVAL_HOURS
    )
    mode = "rewrite" if existing and (at_cap or rewrite_due) else "merge"

    async with async_session() as session:
        messages = await get_project_messages_since(
            session, project_id, messages_through, limit=_MESSAGE_FETCH_LIMIT
        )
    material, consumed_through = _render_material(messages)
    if not material.strip() or consumed_through is None:
        return "skipped: no usable material"

    llm, _ = await _resolve_llm(model_id)
    if llm is None:
        return "skipped: no LLM available"

    if mode == "merge":
        raw = await _ask(
            llm,
            _MERGE_SYSTEM_PROMPT,
            f"Existing project memory:\n---\n{existing or '(empty)'}\n---\n\n"
            f"Recent conversations:\n---\n{material}\n---\n\nNew bullets to add:",
        )
        if not raw or raw == _NO_UPDATE_MARKER:
            await _save_meta(store, project_id, consumed_through, last_rewrite_at or now)
            return f"merge: nothing new ({count} messages read)"
        # Add-only is enforced here, not trusted to the prompt: whatever the
        # model returned, only lines that aren't already stated get appended,
        # and the existing memory is carried over verbatim.
        addition, dropped = dedupe_against(existing, raw)
        if not addition:
            await _save_meta(store, project_id, consumed_through, last_rewrite_at or now)
            return f"merge: {dropped} proposed line(s) already present"
        candidate = f"{existing}\n\n{addition}".strip() if existing else addition
        if len(candidate) > _MEMORY_CAP or _count_bullets(candidate) > _MAX_BULLETS:
            # Would overflow — escalate to the mode that is allowed to evict.
            logger.info("project memory: %s merge overflowed, escalating to rewrite", project_id)
            mode = "rewrite"
        else:
            if not await _commit_memory(project_id, candidate, seen_updated_at):
                return "skipped: concurrent write, will retry next pass"
            await _save_meta(store, project_id, consumed_through, last_rewrite_at or now)
            logger.info(
                "project memory merge: %s +%d line(s), %d → %d chars",
                project_id, len(addition.splitlines()), len(existing), len(candidate),
            )
            return f"merge: added {len(addition.splitlines())} line(s)"

    updated = await _ask(
        llm,
        _REWRITE_SYSTEM_PROMPT,
        f"Current project memory:\n---\n{existing[:_MEMORY_CAP] or '(empty)'}\n---\n\n"
        f"Recent conversations:\n---\n{material}\n---\n\nReplacement memory:",
    )
    if not updated or updated == _NO_UPDATE_MARKER or updated.strip() == existing:
        await _save_meta(store, project_id, consumed_through, now)
        return f"rewrite: no change ({count} messages read)"
    if len(updated) > _MEMORY_CAP:
        updated = updated[:_MEMORY_CAP]
    if not await _commit_memory(project_id, updated, seen_updated_at):
        return "skipped: concurrent write, will retry next pass"
    await _save_meta(store, project_id, consumed_through, now)
    logger.info(
        "project memory rewrite: %s %d → %d chars (%d → %d bullets)",
        project_id, len(existing), len(updated), _count_bullets(existing), _count_bullets(updated),
    )
    return f"rewrite: {len(existing)} → {len(updated)} chars"


# ── the sweep ─────────────────────────────────────────────────────────────────

async def consolidate_project_memories(
    store: AsyncSqliteStore, model_id: str | None = None
) -> str:
    """Consolidate every project that has gone quiet with new material.

    Registered as an interval job (core/scheduler.py). Per-project failures are
    logged and skipped — one bad project must not stop the sweep.
    """
    async with async_session() as session:
        projects = await list_projects(session)
    if not projects:
        return "no projects"

    results: list[str] = []
    touched = 0
    for proj in projects:
        if touched >= _MAX_PROJECTS_PER_TICK:
            logger.info(
                "project memory sweep: hit the %d-project cap, remainder next tick",
                _MAX_PROJECTS_PER_TICK,
            )
            break
        try:
            outcome = await consolidate_project_memory(store, proj.id, model_id=model_id)
        except Exception as exc:
            logger.warning("project memory: %s failed: %s", proj.id, exc, exc_info=True)
            continue
        if not outcome.startswith("skipped"):
            touched += 1
            results.append(f"{proj.name}: {outcome}")

    if not results:
        return f"nothing to do ({len(projects)} project(s) checked)"
    return "; ".join(results)


# ── deprecated ────────────────────────────────────────────────────────────────

async def maybe_auto_maintain_project_memory(*args, **kwargs) -> bool:
    """DEPRECATED — the per-turn hook this replaced fired after every chat.

    Superseded by `consolidate_project_memories`, which batches over the
    messages table instead of re-reading one conversation's tail every turn.
    Kept as a no-op so any stale caller fails quiet rather than loud.
    """
    logger.debug("maybe_auto_maintain_project_memory is a no-op; use consolidate_project_memories")
    return False
