"""Agent memory tools — save and retrieve discrete long-term memory items.

These operate on the global ``Memory`` store (``db/models.Memory`` via
``core/memory_store.py``), not a single conversation. They are only useful when
an embedder is configured; without one the agent's memory is the single
``AGENTS.md`` blob and these tools report that they're unavailable.
"""

from __future__ import annotations

import json
import logging

from langchain_core.tools import tool

from core.doc_index import embeddings_available
from core.memory_store import search_memory as _search_memory
from core.memory_store import upsert_memory

logger = logging.getLogger(__name__)


@tool
async def remember(text: str, kind: str = "fact") -> str:
    """Persist a durable fact to long-term memory, recalled in future conversations.

    Use when the user shares something worth remembering across sessions — a
    preference, ongoing project, or important fact. Not for transient,
    conversation-only details.

    Args:
        text: One atomic, self-contained fact.
        kind: "core" for durable identity/preferences always kept in mind, or
            "fact" (default) for everything else.
    """
    if not text.strip():
        return "Nothing to remember (empty text)."
    from tools.context import current_ctx

    if current_ctx().ephemeral:
        # Incognito conversations must not write durable memory.
        return "Skipped: this is an incognito chat, nothing is saved to long-term memory."
    if not embeddings_available():
        return "Long-term memory is unavailable (no embedding model configured)."
    k = kind if kind in ("core", "fact") else "fact"
    try:
        mid = await upsert_memory(text, k)
    except Exception as exc:
        logger.warning("remember failed: %s", exc)
        return f"Could not save memory: {exc}"
    if mid is None:
        return "Long-term memory is unavailable (no embedding model configured)."
    return f"Remembered ({k})."


@tool
async def search_memory(query: str, k: int = 6) -> str:
    """Search your long-term memory for facts relevant to a query.

    The most relevant memories are already injected automatically each turn;
    use this to dig for something specific that may not have surfaced — a past
    decision, a preference, a project detail. Phrase `query` as the content you
    want to find, not a question.

    Args:
        query: What to look for (a descriptive phrase works best).
        k: How many items to return (default 6).
    """
    if not embeddings_available():
        return "Long-term memory search is unavailable (no embedding model configured)."
    try:
        # Explicit search via tool — log with source explicit_search for audit
        from tools.context import current_ctx

        conv_id = None
        try:
            conv_id = current_ctx().conversation_id
        except Exception:
            conv_id = None

        hits = await _search_memory(query, k=k, conversation_id=conv_id, source="explicit_search")
    except Exception as exc:
        logger.warning("search_memory failed: %s", exc)
        return f"Memory search failed: {exc}"
    if not hits:
        return "No matching memories."
    return json.dumps(hits)
