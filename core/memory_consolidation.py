"""Memory consolidation.

Two write paths, dispatched on whether an embedder is configured:

* **Embedder present** — extract discrete atomic memory items from recent
  conversations and upsert them into the ``Memory`` table (``core/memory_store.py``),
  deduping on insert. A one-time seed splits any legacy ``AGENTS.md`` blob into items.
* **No embedder (keyless/Ollama)** — the original behavior: rewrite a single
  free-text ``AGENTS.md`` blob in the LangGraph store.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.store.sqlite.aio import AsyncSqliteStore

from core.doc_index import embeddings_available
from core.memory_store import upsert_memory
from core.model_catalog import get_model_spec
from db import async_session
from db.ops import count_memories, get_default_model, get_recent_messages, list_memories

logger = logging.getLogger(__name__)

_MEMORY_NS = ("memory",)
_MEMORY_KEY = "AGENTS.md"
_LEGACY_MEMORY_KEY = "/AGENTS.md"
_META_NS = ("memory_consolidation",)
_META_KEY = "state"


async def _migrate_legacy_key(store: AsyncSqliteStore) -> None:
    """Copy any data at the pre-fix `/AGENTS.md` key onto the canonical key.

    The agent's runtime reader and write_file tool always used "AGENTS.md";
    consolidation + GET /agent-memory used "/AGENTS.md" until the keys were
    unified. The legacy key is left in place as a backup for one release.
    """
    canonical = await store.aget(_MEMORY_NS, _MEMORY_KEY)
    if canonical is not None:
        return
    legacy = await store.aget(_MEMORY_NS, _LEGACY_MEMORY_KEY)
    if legacy is not None:
        await store.aput(_MEMORY_NS, _MEMORY_KEY, legacy.value)


# ── Item-extraction path (embedder present) ────────────────────────────────────

_EXTRACT_SYSTEM_PROMPT = """You extract durable memory items about the user from recent
conversations, so an AI assistant can remember them across sessions.

Output ONLY a JSON array. Each element is an object:
  {"text": "<one atomic, self-contained fact>", "kind": "core" | "fact"}

- "core": durable identity and strong preferences that should ALWAYS be in mind —
  who the user is, their role/expertise, hard preferences, how they want the assistant to behave.
- "fact": everything else worth remembering — project details, decisions, context, one-off facts.

Rules:
- One atomic fact per item. Keep each short and self-contained (no pronouns pointing outside the item).
- Only include NEW information not already covered by the existing memory items shown.
- If a conversation contradicts an existing item, emit the corrected version (it replaces the old one).
- Emit [] if there is nothing new worth remembering.
- Output ONLY the JSON array — no prose, no markdown fences.
"""

_SPLIT_SYSTEM_PROMPT = """You convert an existing free-text memory document into discrete
memory items. Output ONLY a JSON array of objects:
  {"text": "<one atomic, self-contained fact>", "kind": "core" | "fact"}

- "core": durable identity and strong preferences that should ALWAYS be in mind.
- "fact": everything else worth remembering.
- One atomic fact per item; keep each short and self-contained.
- Output ONLY the JSON array — no prose, no markdown fences.
"""


def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _coerce_items(raw: Any) -> list[dict]:
    """Tolerantly parse a JSON array of {text, kind} from an LLM response.

    Extracts the outermost [...] span so surrounding prose / markdown fences
    don't break parsing; drops malformed elements and normalizes `kind`.
    """
    text = _flatten(raw)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        logger.warning("memory extraction: no JSON array in response, skipping")
        return []
    try:
        data = json.loads(text[start : end + 1])
    except Exception:
        logger.warning("memory extraction: could not parse JSON array, skipping")
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for el in data:
        if not isinstance(el, dict):
            continue
        t = str(el.get("text", "")).strip()
        k = el.get("kind", "fact")
        out.append({"text": t, "kind": k if k in ("core", "fact") else "fact"})
    return [it for it in out if it["text"]]


async def _llm_json_items(model_id: str, system_prompt: str, human_content: str) -> list[dict]:
    llm = get_model_spec(model_id).build_llm()
    response = await llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_content),
    ])
    return _coerce_items(response.content)


def _existing_block(existing: list) -> str:
    if not existing:
        return "(none yet)"
    return "\n".join(f"- [{m.kind}] {m.text}" for m in existing)


def _transcript_block(messages: list[dict], cap: int = 16_384) -> str:
    lines: list[str] = []
    total = 0
    for m in messages:
        line = f"[{m['created_at'][:16]}] {m['title']} | {m['role'].upper()}: {m['content'][:500]}"
        if total + len(line) > cap:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


async def _seed_from_blob(store: AsyncSqliteStore, model_id: str) -> int:
    """One-time: split the legacy AGENTS.md blob into items. Returns count written.

    The blob is left in place as a backup (same spirit as _migrate_legacy_key).
    """
    mem_item = await store.aget(_MEMORY_NS, _MEMORY_KEY)
    if mem_item is None:
        return 0
    raw = mem_item.value.get("content", "")
    blob = ("\n".join(raw) if isinstance(raw, list) else raw).strip()
    if not blob:
        return 0
    items = await _llm_json_items(
        model_id,
        _SPLIT_SYSTEM_PROMPT,
        f"Existing memory document:\n---\n{blob[:32_000]}\n---\n\nSplit it into items:",
    )
    written = 0
    for it in items:
        if await upsert_memory(it["text"], it["kind"]):
            written += 1
    logger.info("memory: seeded %d items from legacy AGENTS.md blob", written)
    return written


async def _consolidate_items(store: AsyncSqliteStore, model_id: str | None) -> str:
    """Extract atomic items from recent conversations into the Memory table."""
    meta = await store.aget(_META_NS, _META_KEY)
    last_run_at: datetime | None = None
    if meta is not None:
        ts = meta.value.get("last_run_at")
        if ts:
            last_run_at = datetime.fromisoformat(ts)

    async with async_session() as session:
        if model_id is None:
            model_id = await get_default_model(session)
        messages = await get_recent_messages(session, since=last_run_at, limit=200)
        mem_count = await count_memories(session)

    now_iso = datetime.now(timezone.utc).isoformat()

    # One-time migration: split the legacy blob into items on first run.
    seeded = 0
    if mem_count == 0:
        seeded = await _seed_from_blob(store, model_id)

    async with async_session() as session:
        existing = await list_memories(session)

    if not messages:
        await store.aput(_META_NS, _META_KEY, {"last_run_at": now_iso})
        return (
            f"seeded {seeded} items from blob; no new messages since last run"
            if seeded
            else "skipped: no new messages since last run"
        )

    items = await _llm_json_items(
        model_id,
        _EXTRACT_SYSTEM_PROMPT,
        f"Existing memory items:\n---\n{_existing_block(existing)}\n---\n\n"
        f"Recent conversations ({len(messages)} messages):\n---\n"
        f"{_transcript_block(messages)}\n---\n\nExtract new memory items:",
    )
    written = 0
    for it in items:
        if await upsert_memory(it["text"], it["kind"]):
            written += 1

    await store.aput(_META_NS, _META_KEY, {"last_run_at": now_iso})
    logger.info(
        "memory_consolidation: %d messages → %d items written (+%d seeded)",
        len(messages), written, seeded,
    )
    return f"consolidated {len(messages)} messages → {written} memory items written (+{seeded} seeded)"


# ── Blob path (no embedder — original behavior) ────────────────────────────────

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


async def _consolidate_blob(store: AsyncSqliteStore, model_id: str | None) -> str:
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
    human_content = (
        f"Current AGENTS.md:\n---\n{current_memory or '(empty — first consolidation run)'}\n---\n\n"
        f"Recent conversations ({len(messages)} messages):\n---\n"
        + _transcript_block(messages)
        + "\n---\n\nWrite the updated AGENTS.md:"
    )

    # 5. Call LLM (single-shot, no agent loop)
    llm = get_model_spec(model_id).build_llm()
    response = await llm.ainvoke([
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=human_content),
    ])
    new_memory = _flatten(response.content).strip()[:32_000]

    # 6. Write updated memory back to store
    created_at = (mem_item.value.get("created_at") if mem_item else None) or now_iso
    await store.aput(_MEMORY_NS, _MEMORY_KEY, {
        "content": new_memory,
        "encoding": "utf-8",
        "created_at": created_at,
        "modified_at": now_iso,
    })
    await store.aput(_META_NS, _META_KEY, {"last_run_at": now_iso})

    logger.info("memory_consolidation (blob): %d messages → %d chars", len(messages), len(new_memory))
    return f"consolidated {len(messages)} messages; memory is now {len(new_memory)} chars"


async def consolidate_memory(store: AsyncSqliteStore, model_id: str | None = None) -> str:
    """Update persistent memory from recent conversations.

    Dispatches to the discrete-item path when an embedder is configured, else
    the single-blob path. Always migrates the legacy key first.
    """
    await _migrate_legacy_key(store)
    if embeddings_available():
        return await _consolidate_items(store, model_id)
    return await _consolidate_blob(store, model_id)
