"""Memory consolidation.

`edge/src/consolidate/memory.rs` is a port (the item path) that runs the pass
behind the edge — a change here is made there too.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from core.transcript_store import KvStore

from core.doc_index import embeddings_available
from core.memory_store import upsert_memory
from core.model_catalog import resolve_model_spec
from db import async_session
from db.ops import count_memories, get_messages_since, list_memories, resolve_model

logger = logging.getLogger(__name__)

_MEMORY_NS = ("memory",)
_MEMORY_KEY = "AGENTS.md"
_LEGACY_MEMORY_KEY = "/AGENTS.md"
_META_NS = ("memory_consolidation",)
_META_KEY = "state"

# One LLM call reads at most this much transcript. A pass then keeps going batch
# by batch — each advancing the watermark only past what it actually read — up
# to _MAX_BATCHES_PER_RUN, so a busy stretch is worked through over a few passes
# instead of truncated. The cap bounds what one 6-hourly tick can spend.
_BATCH_CHARS = 16_384
_MAX_BATCHES_PER_RUN = 6
_MSG_CAP = 500
_FETCH_LIMIT = 200

# The cron tick and the `consolidateMemory` mutation can both start a pass, and
# both read from the same watermark — overlapping passes would extract the same
# batch twice. Both run on the server's loop, so one in-process lock suffices.
_run_lock = asyncio.Lock()


async def _migrate_legacy_key(store: KvStore) -> None:
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

_EXTRACT_SYSTEM_PROMPT = """You maintain durable memory items about the user from recent
conversations, so an AI assistant can remember them across sessions.

You are given existing memory items with IDs. You must decide which to ADD, UPDATE, or DELETE
based on the recent transcript.

Output ONLY a JSON array. Each element is an operation:
  {"op": "add", "text": "<one atomic self-contained fact>", "kind": "core" | "fact"}
  {"op": "update", "id": "<existing_id>", "text": "<corrected version>", "kind": "core" | "fact"}
  {"op": "delete", "id": "<existing_id>", "reason": "<why — contradicted|temporary_expired|user_requested|outdated>"}

- "core": durable identity and strong preferences that should ALWAYS be in mind —
  who the user is, their role/expertise, hard preferences, how they want the assistant to behave.
- "fact": everything else worth remembering — project details, decisions, context, one-off facts.

Rules for ADD / UPDATE:
- One atomic fact per item. Keep each short and self-contained (no pronouns pointing outside the item).
- Only ADD information not already covered by existing items.
- If transcript contradicts an existing item, emit UPDATE with corrected version pointing to its id.
- DO NOT ADD temporary facts: if user says "for today only", "this week only", "temporarily", "just for now",
  "until Friday", "for this session", don't create a durable memory. If such a temporary fact already
  exists, DELETE it with reason temporary_expired.
- If user says "forget that", "don't remember X", "remove that memory", DELETE the matching id with reason user_requested.

Rules for DELETE:
- Delete when: contradicted by newer info, is temporary and no longer relevant, user explicitly asked to forget,
  or clearly outdated (e.g., job changed, moved, preference reversed).
- Be conservative: only delete when transcript explicitly contradicts or user requested. Don't mass-delete.
- Include reason: contradicted | temporary_expired | user_requested | outdated

If nothing to do, emit [].
Output ONLY the JSON array — no prose, no markdown fences.
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
    """Tolerantly parse a JSON array of {text, kind} / {op,...} from an LLM response.

    Supports both legacy format (no op) and new format (add/update/delete ops).
    Extracts the outermost [...] span so surrounding prose / markdown fences
    don't break parsing; drops malformed elements and normalizes fields.
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
        op = el.get("op", "add")
        if op not in ("add", "update", "delete"):
            # legacy: no op field, just {text, kind}
            op = "add"
        if op == "delete":
            did = str(el.get("id", "")).strip()
            reason = str(el.get("reason", "")).strip() or "unknown"
            if not did:
                continue
            out.append({"op": "delete", "id": did, "reason": reason})
        elif op == "update":
            did = str(el.get("id", "")).strip()
            t = str(el.get("text", "")).strip()
            if not did or not t:
                continue
            k = el.get("kind", "fact")
            out.append({"op": "update", "id": did, "text": t, "kind": k if k in ("core", "fact") else "fact"})
        else:  # add
            t = str(el.get("text", "")).strip()
            if not t:
                continue
            k = el.get("kind", "fact")
            out.append({"op": "add", "text": t, "kind": k if k in ("core", "fact") else "fact"})
    return out


async def _llm_json_items(model_id: str, system_prompt: str, human_content: str) -> list[dict]:
    llm = resolve_model_spec(model_id).build_llm()
    response = await llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_content),
    ])
    return _coerce_items(response.content)


def _existing_block(existing: list) -> str:
    if not existing:
        return "(none yet)"
    # Include id so LLM can reference for update/delete; truncate id display to full but keep short text
    return "\n".join(f'- id={m.id} [{m.kind}] {m.text}' for m in existing)


def _aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; the watermark is compared in UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _transcript_block(
    messages: list[dict], cap: int = _BATCH_CHARS
) -> tuple[str, datetime | None, int]:
    """Render one batch: (transcript, consumed_through, messages_consumed).

    `messages` must be oldest-first. Stops at the char budget, and *before* the
    first reply still being written (`status == "running"`): that row was created
    when its run started, so a watermark past it would never come back for the
    finished text. `consumed_through` is the newest timestamp that made it in —
    the furthest the caller may advance — and None when nothing could be read.
    """
    lines: list[str] = []
    total = 0
    consumed_through: datetime | None = None
    for m in messages:
        if m.get("status") == "running":
            break
        stamp = _aware(m["created_at"])
        line = f"[{stamp:%Y-%m-%d %H:%M}] {m['title']} | {m['role'].upper()}: {(m['content'] or '')[:_MSG_CAP]}"
        if total + len(line) > cap and lines:
            break
        lines.append(line)
        total += len(line)
        consumed_through = stamp
    return "\n".join(lines), consumed_through, len(lines)


async def _load_watermark(store: KvStore) -> datetime | None:
    """The created_at of the last message consolidated.

    Falls back to `last_run_at`, which is what installs before the watermark
    stored: it was the time of the last run, so every message older than it was
    already handled (or, under the old fetch, skipped for good) — either way not
    something to re-read.
    """
    meta = await store.aget(_META_NS, _META_KEY)
    if meta is None:
        return None
    raw = meta.value.get("messages_through") or meta.value.get("last_run_at")
    return _aware(datetime.fromisoformat(raw)) if raw else None


async def _save_watermark(store: KvStore, messages_through: datetime) -> None:
    await store.aput(
        _META_NS,
        _META_KEY,
        {
            "messages_through": messages_through.isoformat(),
            "last_run_at": datetime.now(timezone.utc).isoformat(),
        },
    )


async def _seed_from_blob(store: KvStore, model_id: str) -> int:
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
        txt = it.get("text", "")
        kind = it.get("kind", "fact")
        if txt and await upsert_memory(txt, kind):
            written += 1
    logger.info("memory: seeded %d items from legacy AGENTS.md blob", written)
    return written


async def _consolidate_items(store: KvStore, model_id: str | None) -> str:
    """Extract, update, and delete atomic items based on recent conversations.

    The LLM now emits explicit ops: add / update / delete.
    Temporary memories (e.g. 'for today only') are actively removed.

    Works through unconsolidated messages oldest-first, one budgeted batch per
    LLM call, saving the watermark after each — see _BATCH_CHARS. Oldest-first
    also makes contradictions resolve the right way: a later batch sees the
    items an earlier one wrote and can update them.
    """
    watermark = await _load_watermark(store)

    async with async_session() as session:
        model_id = await resolve_model(model_id, session)
        mem_count = await count_memories(session)

    seeded = 0
    if mem_count == 0:
        seeded = await _seed_from_blob(store, model_id)
    # Sized once per pass from the store as it stood, not per batch.
    max_delete = max(5, int((mem_count + seeded) * 0.3))

    consumed = added = updated = deleted = batches = 0
    backlog = False
    while True:
        async with async_session() as session:
            messages = await get_messages_since(session, since=watermark, limit=_FETCH_LIMIT)
            existing = await list_memories(session)
        transcript, consumed_through, n = _transcript_block(messages)
        if consumed_through is None:
            break
        if batches == _MAX_BATCHES_PER_RUN:
            backlog = True
            break

        ops = await _llm_json_items(
            model_id,
            _EXTRACT_SYSTEM_PROMPT,
            f"Existing memory items:\n---\n{_existing_block(existing)}\n---\n\n"
            f"Recent conversations ({n} messages):\n---\n"
            f"{transcript}\n---\n\n"
            f"Decide add/update/delete operations:",
        )
        a, u, d = await _apply_ops(ops, {m.id for m in existing}, max_delete - deleted)
        added, updated, deleted = added + a, updated + u, deleted + d
        # Saved per batch, so a failure in a later one keeps this one's progress.
        watermark = consumed_through
        await _save_watermark(store, watermark)
        consumed += n
        batches += 1

    if not batches:
        return (
            f"seeded {seeded} items from blob; no new messages since last run"
            if seeded
            else "skipped: no new messages since last run"
        )
    logger.info(
        "memory_consolidation: %d messages in %d batch(es) → +%d ~%d -%d (+%d seeded)%s",
        consumed, batches, added, updated, deleted, seeded,
        "; backlog remains" if backlog else "",
    )
    return (
        f"consolidated {consumed} messages in {batches} batch(es) → "
        f"+{added} ~{updated} -{deleted} (+{seeded} seeded)"
        + ("; backlog remains for the next run" if backlog else "")
    )


async def _apply_ops(
    ops: list[dict], existing_ids: set[str], max_delete: int
) -> tuple[int, int, int]:
    """Apply one batch's add/update/delete ops. Returns (added, updated, deleted).

    `max_delete` is what is left of the run's deletion budget, so the cap holds
    across every batch of a pass rather than resetting per LLM call.
    """
    from core.memory_store import delete_memory_by_id, update_memory_with_embedding

    # Safety: cap deletions per run to avoid catastrophic hallucinated wipe
    if len([o for o in ops if o.get("op") == "delete"]) > max_delete:
        logger.warning(
            "memory_consolidation: LLM wants to delete %d > cap %d, truncating",
            len([o for o in ops if o.get("op") == "delete"]),
            max_delete,
        )
        # Keep only first max_delete deletes
        seen_del = 0
        filtered: list[dict] = []
        for o in ops:
            if o.get("op") == "delete":
                if seen_del >= max_delete:
                    continue
                seen_del += 1
            filtered.append(o)
        ops = filtered

    added = 0
    updated = 0
    deleted = 0

    for it in ops:
        op = it.get("op", "add")
        if op == "delete":
            did = it.get("id")
            # `ops` is LLM-generated JSON, so `id` can be any type or absent.
            # The isinstance check is what the set-membership test was already
            # relying on implicitly — a non-str could never match a str id.
            if not isinstance(did, str) or did not in existing_ids:
                logger.debug("memory_consolidation: skip delete id=%s not in existing", did)
                continue
            reason = it.get("reason", "unknown")
            if await delete_memory_by_id(did):
                deleted += 1
                existing_ids.discard(did)
                logger.info("memory_consolidation: deleted %s reason=%s", did, reason)
            continue
        if op == "update":
            did = it.get("id")
            if not isinstance(did, str) or did not in existing_ids:
                # id not found (or not a string) — treat as add
                text = it.get("text", "")
                kind = it.get("kind", "fact")
                if text and await upsert_memory(text, kind):
                    added += 1
                continue
            if await update_memory_with_embedding(did, it["text"], it.get("kind", "fact")):
                updated += 1
            continue
        # add
        text = it.get("text", "")
        kind = it.get("kind", "fact")
        if not text:
            continue
        if await upsert_memory(text, kind):
            added += 1

    return added, updated, deleted


# ── Blob path (no embedder — original behavior) ────────────────────────────────

_SYSTEM_PROMPT = """You are a memory consolidation assistant. Your job is to update
a persistent AGENTS.md memory document that helps an AI assistant remember key facts
about the user across conversations.

Rules:
- Keep the document under 200 lines of markdown
- Organize with headers: ## User Preferences, ## Ongoing Projects, ## Key Facts, ## Context
- Merge new information with existing memory; preserve prior facts unless clearly contradicted
- DELETE when: contradicted by newer info, user said "forget that", or fact is temporary ("for today only", "this week only", "temporarily", "just for now", "until X", "for this session") and no longer relevant
- DO NOT persist temporary facts — if user says "for today only", don't add it; if such exists, remove it
- When user explicitly asks to forget, remove matching lines
- Be concise — bullet points of facts, not prose
- Output ONLY the updated markdown document — no preamble, no explanation
"""


async def _consolidate_blob(store: KvStore, model_id: str | None) -> str:
    """Read unconsolidated DB messages + current AGENTS.md, call LLM to update memory, write back.

    Same batching and watermark as _consolidate_items: one budgeted batch per
    LLM call, oldest first, each folding into the document the previous one wrote.
    """
    watermark = await _load_watermark(store)
    async with async_session() as session:
        model_id = await resolve_model(model_id, session)

    consumed = batches = 0
    backlog = False
    new_memory = ""
    llm = None
    while True:
        async with async_session() as session:
            messages = await get_messages_since(session, since=watermark, limit=_FETCH_LIMIT)
        transcript, consumed_through, n = _transcript_block(messages)
        if consumed_through is None:
            break
        if batches == _MAX_BATCHES_PER_RUN:
            backlog = True
            break

        mem_item = await store.aget(_MEMORY_NS, _MEMORY_KEY)
        current_memory = ""
        if mem_item is not None:
            raw = mem_item.value.get("content", "")
            current_memory = "\n".join(raw) if isinstance(raw, list) else raw
        current_memory = current_memory[:8192]

        human_content = (
            f"Current AGENTS.md:\n---\n{current_memory or '(empty — first consolidation run)'}\n---\n\n"
            f"Recent conversations ({n} messages):\n---\n"
            + transcript
            + "\n---\n\nWrite the updated AGENTS.md:"
        )

        # Single-shot, no agent loop.
        llm = llm or resolve_model_spec(model_id).build_llm()
        response = await llm.ainvoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=human_content),
        ])
        new_memory = _flatten(response.content).strip()[:32_000]

        now_iso = datetime.now(timezone.utc).isoformat()
        created_at = (mem_item.value.get("created_at") if mem_item else None) or now_iso
        await store.aput(_MEMORY_NS, _MEMORY_KEY, {
            "content": new_memory,
            "encoding": "utf-8",
            "created_at": created_at,
            "modified_at": now_iso,
        })
        watermark = consumed_through
        await _save_watermark(store, watermark)
        consumed += n
        batches += 1

    if not batches:
        return "skipped: no new messages since last run"
    logger.info(
        "memory_consolidation (blob): %d messages in %d batch(es) → %d chars%s",
        consumed, batches, len(new_memory), "; backlog remains" if backlog else "",
    )
    return (
        f"consolidated {consumed} messages in {batches} batch(es); "
        f"memory is now {len(new_memory)} chars"
        + ("; backlog remains for the next run" if backlog else "")
    )


async def consolidate_memory(store: KvStore, model_id: str | None = None) -> str:
    """Update persistent memory from recent conversations.

    Dispatches to the discrete-item path when an embedder is configured, else
    the single-blob path. Always migrates the legacy key first.
    """
    if _run_lock.locked():
        return "skipped: a consolidation pass is already running"
    async with _run_lock:
        await _migrate_legacy_key(store)
        if embeddings_available():
            return await _consolidate_items(store, model_id)
        return await _consolidate_blob(store, model_id)
