"""Project memory auto-maintenance — safety net when agent skips project_memory."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage, SystemMessage

from db.engine import async_session
from db.models import Project
from db.ops import get_default_model, update_project

logger = logging.getLogger(__name__)

_MEMORY_CAP = 24_000
# Conservative thresholds — only trigger on substantive conversations
_INIT_MIN_CHARS = 400
_REFRESH_MIN_CHARS = 800
_STALE_DAYS = 14
_RECENT_UPDATE_MINUTES = 10
_NO_UPDATE_MARKER = "__NO_UPDATE__"

_SYSTEM_PROMPT = """You initialize project memory — a shared notepad every conversation in THIS project will see.

Goal: Extract ONLY facts that are explicitly tied to THIS specific project.

Rules:
- Output ONLY markdown bullet list or short paragraphs — no preamble, no explanation, no code fences.
- Keep it under 80 lines, concise bullets.
- If transcript has no project-specific durable facts, output empty string.

What to SAVE (project-specific ONLY — must be explicitly mentioned for THIS project):
- Tech stack & versions used in THIS project (e.g., "This project uses FastAPI + React 19")
- Architecture decisions made for THIS project
- Coding conventions specific to THIS project (not global prefs)
- Important file paths/modules in THIS project
- API contracts / endpoints defined in THIS project
- Goals, status, todos specific to THIS project

CRITICAL — DO NOT SAVE (these belong to global memory via `remember`, not project memory):
- User's personal info: name, role, background, location
- General communication preferences: "likes concise answers", "prefers detailed explanations"
- Global coding preferences: "prefers pnpm", "always use type hints" UNLESS explicitly tied to this project ("for this project we use pnpm")
- General knowledge, small talk, greetings
- Any fact not explicitly tied to THIS project by context

If transcript only contains general user info, small talk, or global preferences with NO project-specific facts, output empty string.
- Never invent — only facts present in transcript.
- Don't include secrets, tokens, credentials.
"""

_REFRESH_SYSTEM_PROMPT = """You maintain project memory — a shared notepad for THIS project only.

You are given:
1. Existing project memory for THIS project
2. New conversation transcript from THIS project

Task: Merge any NEW project-specific durable facts from transcript into existing memory, prune outdated.

Rules:
- Output ONLY the updated full project memory — markdown bullets/short paras, no preamble, no fences.
- If transcript has no NEW project-specific facts beyond existing memory, output exactly: __NO_UPDATE__
- Keep under 80 lines, concise. Preserve existing facts unless contradicted by transcript.
- If transcript fact contradicts existing memory, prefer transcript.

What to SAVE (only project-specific, explicitly tied to THIS project):
- Tech stack & versions for THIS project
- Architecture decisions for THIS project
- Coding conventions specific to THIS project
- Important files/modules, API contracts in THIS project
- Project goals/status/todos

CRITICAL — DO NOT INCLUDE (belongs to global memory):
- User's personal info, background, general prefs
- Global coding style that applies to ALL projects
- General communication preferences
- Small talk, greetings, non-project facts

If nothing new project-specific, output __NO_UPDATE__.
- Never invent beyond transcript+existing memory. Don't include secrets/tokens.
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
        # Short transcripts never qualify for either path
        min_needed = _INIT_MIN_CHARS if mode != "refresh" else _REFRESH_MIN_CHARS
        if transcript_len < min_needed:
            logger.debug("project_memory auto: transcript too short (%d < %d) for %s", transcript_len, min_needed, project_id)
            return False

        async with async_session() as session:
            proj = await session.get(Project, project_id)
            if proj is None:
                logger.debug("project_memory auto: project %s not found", project_id)
                return False
            existing = (proj.memory or "").strip()
            updated_at = proj.updated_at

        is_empty = not existing

        # Guard: if memory was updated very recently (agent just called project_memory), skip auto
        if updated_at is not None:
            now = datetime.now(timezone.utc)
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            minutes_since = (now - updated_at).total_seconds() / 60
            if minutes_since < _RECENT_UPDATE_MINUTES:
                logger.debug(
                    "project_memory auto: skip recent update (%.1f min ago) for %s",
                    minutes_since,
                    project_id,
                )
                return False

        # Decide which path
        should_init = is_empty and mode in ("auto", "init")
        should_refresh = False

        if not is_empty and mode in ("auto", "refresh"):
            # stale check: updated_at older than _STALE_DAYS AND transcript large enough
            if updated_at is None:
                should_refresh = transcript_len >= _REFRESH_MIN_CHARS
            else:
                now = datetime.now(timezone.utc)
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                age_days = (now - updated_at).total_seconds() / 86400
                if age_days > _STALE_DAYS and transcript_len >= _REFRESH_MIN_CHARS:
                    should_refresh = True
                else:
                    logger.debug(
                        "project_memory auto: not stale (age %.1f days, need %d) for %s",
                        age_days if updated_at else 0,
                        _STALE_DAYS,
                        project_id,
                    )

        if not should_init and not should_refresh:
            logger.debug(
                "project_memory auto: skip project=%s empty=%s mode=%s len=%d",
                project_id,
                is_empty,
                mode,
                transcript_len,
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
