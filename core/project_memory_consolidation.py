"""Project memory auto-initialization — safety net when agent skips project_memory.

Phase 1 strengthened prompts; this module provides a fallback that guarantees
project memory gets initialized on the first substantial conversation,
even if the main agent forgets to call the tool.

It mirrors the pattern in core/memory_consolidation.py but is:
- scoped to a single Project (not global)
- triggered only when memory is empty
- fire-and-forget from chat_runtime (asyncio.create_task) so it never
  blocks the user-visible response

Trigger: server/chat_runtime.py after a successful chat run (status=done)
when project_id is set and current memory is empty.

LLM call is single-shot (no agent loop), capped, and best-effort — any
failure is logged but does not affect the chat run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage

from db.engine import async_session
from db.models import Project
from db.ops import get_default_model, update_project

logger = logging.getLogger(__name__)

_MEMORY_CAP = 24_000

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
        # Trim but keep latest partially
        remaining = cap - total
        if remaining > 200:
            parts.append(latest[:remaining])

    return "".join(parts)


async def maybe_initialize_project_memory(
    project_id: str,
    conv_id: str,
    query: str,
    final_message: str,
    model_id: str | None = None,
) -> bool:
    """Auto-initialize project memory if empty.

    Returns True if memory was written, False otherwise (already non-empty,
    too short, LLM returned empty, or error). Best-effort — never raises.
    """
    try:
        # 1. Check if memory is empty — fast path
        async with async_session() as session:
            proj = await session.get(Project, project_id)
            if proj is None:
                logger.debug("project_memory auto-init: project %s not found", project_id)
                return False
            if proj.memory and proj.memory.strip():
                # Already initialized — main agent handled it
                return False

        transcript = (query or "").strip() + "\n" + (final_message or "").strip()
        if len(transcript.strip()) < 100:
            logger.debug("project_memory auto-init: transcript too short for %s", project_id)
            return False

        # 2. Optionally enrich with recent messages from this conversation
        conv_excerpt: list[dict] | None = None
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
                    conv_excerpt = [
                        {"role": r.role, "content": r.content[:800]} for r in reversed(rows)
                    ]
        except Exception:
            # Enrichment is best-effort
            conv_excerpt = None

        built = _build_transcript(query, final_message, conv_excerpt)

        # 3. Resolve model
        if model_id is None:
            async with async_session() as session:
                model_id = await get_default_model(session)

        from core.model_catalog import get_model_spec

        try:
            llm = get_model_spec(model_id).build_llm()
        except Exception as exc:
            logger.warning("project_memory auto-init: cannot build LLM %s: %s", model_id, exc)
            return False

        # 4. LLM call — single shot
        response = await llm.ainvoke(
            [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=f"Transcript:\n---\n{built}\n---\n\nWrite project memory (or empty string if nothing durable):"),
            ]
        )

        raw = response.content
        if isinstance(raw, list):
            new_memory = "".join(b.get("text", "") for b in raw if isinstance(b, dict)).strip()
        else:
            new_memory = str(raw).strip()

        if not new_memory or len(new_memory) < 20:
            logger.info("project_memory auto-init: LLM returned no durable facts for project %s", project_id)
            return False

        if len(new_memory) > _MEMORY_CAP:
            new_memory = new_memory[:_MEMORY_CAP]

        # 5. Double-check before write (race guard) — another turn may have written meanwhile
        async with async_session() as session:
            proj = await session.get(Project, project_id)
            if proj is None:
                return False
            if proj.memory and proj.memory.strip():
                logger.debug("project_memory auto-init: memory filled by race for %s", project_id)
                return False
            await update_project(session, project_id, memory=new_memory)

        logger.info("project_memory auto-init: initialized project %s with %d chars", project_id, len(new_memory))
        return True

    except Exception as exc:
        logger.warning("project_memory auto-init failed for project %s: %s", project_id, exc, exc_info=True)
        return False
