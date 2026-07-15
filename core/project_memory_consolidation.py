"""Project memory auto-maintenance — safety net when agent skips project_memory.

Two-layer guarantee:
- **Layer 1 (Phase 1):** Strong prompts in system_prompt.md + volatile header
  tell the agent to actively maintain memory.
- **Layer 2 (Phase 2 — this module):** Best-effort background LLM that
  guarantees memory gets initialized on first substantial conversation
  and refreshed when stale, even if the main agent forgets.

It mirrors the pattern in core/memory_consolidation.py but is:
- scoped to a single Project (not global)
- triggered for empty OR stale memory
- fire-and-forget from chat_runtime (asyncio.create_task) so it never
  blocks the user-visible response

Trigger: server/chat_runtime.py after a successful chat run (status=done)
when project_id is set. Init when empty, refresh when older than
_STALE_DAYS and new transcript is substantive.

LLM calls are single-shot (no agent loop), capped, and best-effort —
any failure is logged but does not affect the chat run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage, SystemMessage

from db.engine import async_session
from db.models import Project
from db.ops import get_default_model, update_project

logger = logging.getLogger(__name__)

_MEMORY_CAP = 24_000
_INIT_MIN_CHARS = 100
_REFRESH_MIN_CHARS = 200
_STALE_DAYS = 7
_NO_UPDATE_MARKER = "__NO_UPDATE__"

_SYSTEM_PROMPT = """You initialize project memory — a shared notepad every conversation in this project will see.

Given a conversation transcript from the project, extract durable, project-wide facts worth remembering.

Rules:
- Output ONLY markdown bullet list or short paragraphs — no preamble, no explanation, no code fences.
- Focus on: tech stack & versions, architecture decisions, coding conventions, important file paths/modules, API contracts, user preferences specific to this project, current goals/status.
- Skip transient details of this one chat (e.g. "user asked X") — keep only facts that future conversations will need.
- Keep it under 100 lines, concise bullets. If transcript has no durable facts, output empty string.
- Never invent — only facts present in the transcript.
- Don't include secrets, tokens, or credentials.
"""

_REFRESH_SYSTEM_PROMPT = """You maintain project memory — a shared notepad every conversation in this project sees.

You are given:
1. Existing project memory (may be outdated/incomplete)
2. New conversation transcript from this project

Task: Merge any new durable facts from transcript into existing memory, and prune outdated/conflicting entries. 

Rules:
- Output ONLY the updated full project memory — markdown bullets/short paras, no preamble, no fences.
- If transcript has no new durable facts beyond what's already in memory, output exactly: __NO_UPDATE__
- Keep under 100 lines, concise. Preserve important existing facts unless contradicted.
- Focus on: tech stack & versions, architecture decisions, coding conventions, important files/modules, API contracts, user prefs for this project, goals/status/remaining work.
- Never invent beyond transcript+existing memory. Don't include secrets/tokens.
- If a fact in existing memory is contradicted by transcript, prefer transcript.
"""


def _build_transcript(query: str, final_message: str, conv_messages: list[dict] | None = None, cap: int = 12_000) -> str:
    """Build a transcript excerpt capped to cap chars."""
    parts: list[str] = []
    total = 0

    if conv_messages:
        for m in conv_messages:
            role = m.get("role", "unknown").upper()
            content = (m.get("content") or "")[:800]
            line = f"{role}: {content}\n"
            if total + len(line) > cap:
                break
            parts.append(line)
            total += len(line)

    # Always include the latest turn (may duplicate if already in history, that's ok)
    latest = f"USER: {query}\n\nASSISTANT: {final_message}\n"
    if total + len(latest) <= cap:
        parts.append(latest)
    else:
        remaining = cap - total
        if remaining > 200:
            parts.append(latest[:remaining])

    return "".join(parts)


def _coerce_text(raw) -> str:
    if isinstance(raw, list):
        return "".join(b.get("text", "") for b in raw if isinstance(b, dict)).strip()
    return str(raw).strip()


async def _gather_excerpt(conv_id: str) -> list[dict] | None:
    try:
        from sqlalchemy import select
        from db.models import Message

        async with async_session() as session:
            q = (
                select(Message.role, Message.content)
                .where(Message.conversation_id == conv_id)
                .order_by(Message.created_at.desc())
                .limit(8)
            )
            rows = (await session.execute(q)).all()
            if rows:
                return [{"role": r.role, "content": r.content[:800]} for r in reversed(rows)]
    except Exception:
        return None
    return None


async def _resolve_llm(model_id: str | None):
    if model_id is None:
        async with async_session() as session:
            model_id = await get_default_model(session)
    from core.model_catalog import get_model_spec

    try:
        return get_model_spec(model_id).build_llm(), model_id
    except Exception as exc:
        logger.warning("project_memory auto-maintain: cannot build LLM %s: %s", model_id, exc)
        return None, model_id


async def maybe_initialize_project_memory(
    project_id: str,
    conv_id: str,
    query: str,
    final_message: str,
    model_id: str | None = None,
) -> bool:
    """Backward-compat alias — now delegates to auto-maintain (empty-only path)."""
    return await maybe_auto_maintain_project_memory(
        project_id, conv_id, query, final_message, model_id, mode="init"
    )


async def maybe_auto_maintain_project_memory(
    project_id: str,
    conv_id: str,
    query: str,
    final_message: str,
    model_id: str | None = None,
    mode: str = "auto",  # "auto" | "init" | "refresh"
) -> bool:
    """Auto-maintain project memory (init when empty, refresh when stale).

    - init: only when memory is empty
    - refresh: only when memory is stale (>_STALE_DAYS) and substantive new transcript
    - auto: tries init first, then refresh if not empty

    Returns True if memory was written.
    """
    try:
        transcript_len = len((query or "").strip() + "\n" + (final_message or "").strip())
        if transcript_len < _INIT_MIN_CHARS:
            logger.debug("project_memory auto: transcript too short for %s", project_id)
            return False

        async with async_session() as session:
            proj = await session.get(Project, project_id)
            if proj is None:
                logger.debug("project_memory auto: project %s not found", project_id)
                return False
            existing = (proj.memory or "").strip()
            updated_at = proj.updated_at

        is_empty = not existing

        # Decide which path
        should_init = is_empty and mode in ("auto", "init")
        should_refresh = False

        if not is_empty and mode in ("auto", "refresh"):
            # stale check: updated_at older than _STALE_DAYS
            if updated_at is None:
                should_refresh = True
            else:
                # ensure aware datetime
                now = datetime.now(timezone.utc)
                # updated_at may be naive — treat as UTC
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                if now - updated_at > timedelta(days=_STALE_DAYS):
                    should_refresh = transcript_len >= _REFRESH_MIN_CHARS

        if not should_init and not should_refresh:
            if is_empty:
                # empty but transcript too short already handled, or mode=refresh
                return False
            logger.debug(
                "project_memory auto: skip project=%s empty=%s stale_days_check mode=%s",
                project_id,
                is_empty,
                mode,
            )
            return False

        excerpt = await _gather_excerpt(conv_id)
        built = _build_transcript(query, final_message, excerpt)

        llm, resolved_model = await _resolve_llm(model_id)
        if llm is None:
            return False

        if should_init:
            response = await llm.ainvoke(
                [
                    SystemMessage(content=_SYSTEM_PROMPT),
                    HumanMessage(
                        content=f"Transcript:\n---\n{built}\n---\n\nWrite project memory (or empty string if nothing durable):"
                    ),
                ]
            )
            new_memory = _coerce_text(response.content)
            if not new_memory or len(new_memory) < 20:
                logger.info("project_memory auto-init: no durable facts for %s", project_id)
                return False
            if len(new_memory) > _MEMORY_CAP:
                new_memory = new_memory[:_MEMORY_CAP]

            async with async_session() as session:
                proj = await session.get(Project, project_id)
                if proj is None or (proj.memory and proj.memory.strip()):
                    logger.debug("project_memory auto-init race for %s", project_id)
                    return False
                await update_project(session, project_id, memory=new_memory)

            logger.info("project_memory auto-init: initialized %s with %d chars", project_id, len(new_memory))
            return True

        # refresh path
        response = await llm.ainvoke(
            [
                SystemMessage(content=_REFRESH_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"Existing project memory:\n---\n{existing[:_MEMORY_CAP]}\n---\n\n"
                        f"New transcript:\n---\n{built}\n---\n\n"
                        f"Write updated memory, or { _NO_UPDATE_MARKER } if nothing new:"
                    )
                ),
            ]
        )
        updated = _coerce_text(response.content)

        if not updated or updated == _NO_UPDATE_MARKER or len(updated) < 20:
            logger.info("project_memory auto-refresh: no new facts for %s", project_id)
            return False

        if len(updated) > _MEMORY_CAP:
            updated = updated[:_MEMORY_CAP]

        # Avoid no-op writes (LLM returned identical)
        if updated.strip() == existing:
            logger.debug("project_memory auto-refresh: identical for %s", project_id)
            return False

        async with async_session() as session:
            proj = await session.get(Project, project_id)
            if proj is None:
                return False
            # race guard: if updated in meantime to something else, respect latest? Overwrite is okay for refresh but check still stale-ish
            await update_project(session, project_id, memory=updated)

        logger.info("project_memory auto-refresh: updated %s %d → %d chars", project_id, len(existing), len(updated))
        return True

    except Exception as exc:
        logger.warning("project_memory auto-maintain failed for %s: %s", project_id, exc, exc_info=True)
        return False
