"""Project memory tool — the agent's read/write access to shared project state.

A conversation may belong to a project (Conversation.project_id). Every
conversation in a project shares the project's `instructions` (user-owned,
read-only to the agent) and `memory` (a free-text blob the agent maintains).
Both are injected into the volatile system-prompt suffix each model iteration
(core/agents.py:_project_volatile_parts), so a write here is visible to the
model on its very next LLM call.

Scope comes from current_ctx().project_id — set only by the chat runtime when
the conversation belongs to a project, so the tool politely no-ops everywhere
else (automations, board tasks, bots, non-project chats).
"""

from __future__ import annotations

import logging

from db.engine import async_session
from db.ops import append_project_memory, get_project, update_project
from tools.context import current_ctx

logger = logging.getLogger(__name__)

# Keep the injected volatile suffix bounded — memory beyond this must be
# condensed by the agent (action="write") rather than grown further.
_MEMORY_CAP = 24_000


async def project_memory(action: str, content: str | None = None) -> str:
    """Read or update the shared memory of the project this conversation belongs to.

    A free-text notepad shared by every conversation in the project, injected
    into your system prompt each turn — edits apply on the very next LLM call.
    Store ONLY facts explicitly tied to THIS project: its stack, architecture
    decisions, conventions, key file paths, goals/status. Anything global —
    user info, communication style, general coding prefs — goes to `remember`
    instead. Append project facts as you learn them; initialize the memory if
    it's empty and you have some; use write to condense when it grows stale.

    action: "read" | "append" (add a note) | "write" (replace the whole memory).
    content: required for append/write.
    """
    ctx = current_ctx()
    if not ctx.project_id:
        return "This conversation is not part of a project, so there is no project memory."

    action = action.strip().lower()
    logger.info("project_memory called action=%s project_id=%s has_content=%s", action, ctx.project_id, bool(content and content.strip()))

    if action not in ("read", "append", "write"):
        return f"Error: unknown action '{action}'. Use read, append, or write."

    async with async_session() as session:
        if action == "read":
            proj = await get_project(session, ctx.project_id)
            if proj is None:
                return "Error: project not found."
            logger.info("project_memory read project_id=%s len=%d", ctx.project_id, len(proj.memory or ""))
            return proj.memory if proj.memory.strip() else "Project memory is empty."

        if not content or not content.strip():
            return f"Error: content is required for action '{action}'."

        if action == "append":
            proj = await get_project(session, ctx.project_id)
            if proj is None:
                return "Error: project not found."
            if len(proj.memory) + len(content) > _MEMORY_CAP:
                return (
                    f"Error: appending would exceed the {_MEMORY_CAP}-char project "
                    'memory cap. Use action="write" to replace it with a condensed '
                    "version instead."
                )
            await append_project_memory(session, ctx.project_id, content)
            logger.info("project_memory appended project_id=%s appended_len=%d new_total=%d", ctx.project_id, len(content), len(proj.memory) + len(content))
            return "Appended to project memory."

        # action == "write"
        if len(content) > _MEMORY_CAP:
            return (
                f"Error: content is {len(content)} chars; project memory is capped "
                f"at {_MEMORY_CAP}. Condense it and try again."
            )
        proj = await update_project(session, ctx.project_id, memory=content.strip())
        if proj is None:
            return "Error: project not found."
        logger.info("project_memory write replaced project_id=%s new_len=%d", ctx.project_id, len(content))
        return "Project memory replaced."
