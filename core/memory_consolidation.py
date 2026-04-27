"""Memory consolidation — reads recent conversation history and updates AGENTS.md."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.store.sqlite.aio import AsyncSqliteStore

from core.model_catalog import AVAILABLE_MODELS
from db import async_session
from db.ops import get_default_model, get_recent_messages

logger = logging.getLogger(__name__)

_MEMORY_NS = ("memory",)
_MEMORY_KEY = "/AGENTS.md"
_META_NS = ("memory_consolidation",)
_META_KEY = "state"

_SYSTEM_PROMPT = """You are a memory consolidation assistant. Your job is to update
a persistent AGENTS.md memory document that helps an AI assistant remember key facts
about the user across conversations.

Rules:
- Keep the document under 200 lines of markdown
- Organize with headers: ## User Preferences, ## Ongoing Projects, ## Key Facts, ## Context
- Merge new information with existing memory; preserve prior facts unless clearly contradicted
- Be concise — bullet points of facts, not prose
- Output ONLY the updated markdown document — no preamble, no explanation
"""


async def consolidate_memory(
    store: AsyncSqliteStore,
    model_id: str | None = None,
) -> str:
    """Read recent DB messages + current AGENTS.md, call LLM to update memory, write back."""

    # 1. Read last-run timestamp
    meta = await store.aget(_META_NS, _META_KEY)
    last_run_at: datetime | None = None
    if meta is not None:
        ts = meta.value.get("last_run_at")
        if ts:
            last_run_at = datetime.fromisoformat(ts)

    # 2. Fetch recent messages and resolve model
    async with async_session() as session:
        if model_id is None:
            model_id = await get_default_model(session)
        messages = await get_recent_messages(session, since=last_run_at, limit=200)

    now_iso = datetime.now(timezone.utc).isoformat()

    if not messages:
        await store.aput(_META_NS, _META_KEY, {"last_run_at": now_iso})
        return "skipped: no new messages since last run"

    # 3. Read current AGENTS.md from store
    mem_item = await store.aget(_MEMORY_NS, _MEMORY_KEY)
    current_memory = ""
    if mem_item is not None:
        raw = mem_item.value.get("content", "")
        current_memory = "\n".join(raw) if isinstance(raw, list) else raw
    current_memory = current_memory[:8192]

    # 4. Build transcript excerpt (cap at ~16 KB)
    lines: list[str] = []
    total = 0
    for m in messages:
        line = f"[{m['created_at'][:16]}] {m['title']} | {m['role'].upper()}: {m['content'][:500]}"
        if total + len(line) > 16_384:
            break
        lines.append(line)
        total += len(line)

    human_content = (
        f"Current AGENTS.md:\n---\n{current_memory or '(empty — first consolidation run)'}\n---\n\n"
        f"Recent conversations ({len(messages)} messages):\n---\n"
        + "\n".join(lines)
        + "\n---\n\nWrite the updated AGENTS.md:"
    )

    # 5. Call LLM (single-shot, no agent loop)
    spec = next((m for m in AVAILABLE_MODELS if m.id == model_id), None)
    if spec is None:
        raise ValueError(f"Unknown model '{model_id}'")
    llm = spec.build_llm()
    response = await llm.ainvoke([
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=human_content),
    ])
    new_memory = response.content
    if isinstance(new_memory, list):
        new_memory = next((b["text"] for b in new_memory if isinstance(b, dict) and b.get("type") == "text"), "")  # type: ignore[index]
    new_memory = new_memory.strip()[:32_000]

    # 6. Write updated memory back to store
    created_at = (mem_item.value.get("created_at") if mem_item else None) or now_iso
    await store.aput(_MEMORY_NS, _MEMORY_KEY, {
        "content": new_memory,
        "encoding": "utf-8",
        "created_at": created_at,
        "modified_at": now_iso,
    })
    await store.aput(_META_NS, _META_KEY, {"last_run_at": now_iso})

    logger.info("memory_consolidation: %d messages → %d chars", len(messages), len(new_memory))
    return f"consolidated {len(messages)} messages; memory is now {len(new_memory)} chars"
