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

    Project memory is a free-text notepad shared by every conversation in the
    project. It is injected into your system prompt each turn, so edits are
    visible on the very next LLM call.

    WHEN TO USE — only for facts explicitly tied to THIS project:
    - Tech stack: "Stack for THIS project: FastAPI + Strawberry GraphQL, React 19 + Relay"
    - Architecture decisions: "Decision for THIS project: durable SQLite job queue (Job table) instead of bare asyncio.create_task"
    - Conventions: "Conventions for THIS project: GraphQL-first API; REST only for binary download. pnpm used in this repo."
    - Important paths: "Key files in THIS project: core/agents.py=agent factory, server/chat_runtime.py=chat handler"

    WHEN NOT TO USE — these belong to global memory via `remember`, NOT project_memory:
    - User personal info: name, role, background
    - General communication style: "likes concise answers"
    - Global coding prefs: "prefers pnpm", "always use type hints" unless explicitly "for this project we use..."
    - Any fact not explicitly tied to THIS project context

    Rules:
    - Prefer this over remember ONLY when fact is explicitly tied to THIS project. If global, use remember.
    - If memory empty AND you have project-specific facts, initialize it. Don't init with general prefs.
    - Before finishing, check if THIS project learned something project-specific that future chats need. If outdated, use write to condense.
    - Keep content focused, no general memory pollution.

    Args:
        action: "read" | "append" (add a note at the end) | "write" (replace
            the entire memory — use to reorganize or prune).
        content: The text to append/write. Required for append/write.
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
