"""Discrete agent-memory items: vector retrieval + dedup-on-insert."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import numpy as np

from core.doc_index import get_embedder
from core.retrieval import env_float, fts_match_expr, select_hybrid
from db import async_session
from db.ops import (
    create_memory,
    delete_memory,
    list_memories,
    search_memories_lexical,
    update_memory_item,
)

logger = logging.getLogger(__name__)

# Cosine ≥ this against an existing same-kind item ⇒ treat as the same memory
# and update it in place rather than inserting a near-duplicate. Keeps
# contradictions from piling up the way the whole-doc rewrite handled for free.
# NOTE: this is calibrated to the *current* embedding model's geometry and does
# not transfer across models — re-tune it if `embedding.model` changes.
_DEDUP_THRESHOLD = 0.88

# ── Hybrid retrieval cutoff ───────────────────────────────────────────────────
# How many BM25 candidates to pull before fusion. Only the top-k of these can
# bypass the dense floor, so a larger pool costs a cheap index scan and nothing
# in output quality.
_SPARSE_CANDIDATES = 20

# The "everything here is garbage" floor. This is the load-bearing constant for
# rejecting a whole result set, and it CANNOT be derived a priori — cosine
# distributions are model-specific, and most models compress unrelated pairs
# into a narrow band well above zero. Ship, watch the scores that `select_hybrid`
# logs against real queries, then tune. Raise it until junk stops surfacing; if
# real memories start disappearing, you have gone too far.
_MIN_COSINE = env_float("JARVIS_MEMORY_MIN_COSINE", 0.30)

# Drop anything scoring below this fraction of the best hit — catches the long
# tail when the top result *is* good but the rest are filler.
_REL_DROP = env_float("JARVIS_MEMORY_REL_DROP", 0.75)

# Core memories are always injected — cache them briefly to avoid DB hit
# per turn (trivial queries hit this too). Cap to avoid token bloat.
_CORE_CACHE_TTL = 30  # seconds
_CORE_CACHE_MAX_CHARS = 2000
_core_cache: dict[str, Any] = {"text": "", "ts": 0.0, "count": 0}


def _cosine(a: np.ndarray, anorm: float, b: bytes) -> float | None:
    """Cosine of `a` (with precomputed norm) against stored float32 bytes `b`.

    Returns None when the stored vector was embedded by a different model (shape
    mismatch) — skip rather than crash, mirroring doc_index.search_chunks.
    """
    vec = np.frombuffer(b, dtype=np.float32)
    if vec.shape != a.shape:
        return None
    return float(np.dot(vec, a) / ((float(np.linalg.norm(vec)) or 1.0) * anorm))


async def embed_for_storage(text: str) -> bytes | None:
    """Embed `text` in document space for storage/dedup. None if no embedder."""
    embedder = get_embedder()
    if embedder is None:
        return None
    vecs = await embedder.aembed_documents([text])
    return np.asarray(vecs[0], dtype=np.float32).tobytes()


async def load_core() -> str:
    """All `core` memory items, newline-joined, cached 30s and capped to avoid bloat.

    Always injected into context, but token cost grows with core count. Cache
    avoids DB hit per turn; cap prevents 200 core items = 10k tokens injection.
    """
    now = time.time()
    # Fast path: cached and not expired
    if now - _core_cache["ts"] < _CORE_CACHE_TTL and _core_cache["text"] is not None:
        return _core_cache["text"]

    async with async_session() as session:
        rows = await list_memories(session, kind="core")

    text = "\n".join(f"- {r.text}" for r in rows)

    # Cap chars to avoid token bloat, keep most recent (last rows are newest? order by created_at asc via DB default)
    if len(text) > _CORE_CACHE_MAX_CHARS:
        truncated = text[:_CORE_CACHE_MAX_CHARS].rsplit("\n", 1)[0]
        remaining = len(rows) - truncated.count("\n") - 1
        if remaining > 0:
            truncated += f"\n- ... and {remaining} more core memories (use search_memory to find specific ones)"
        text = truncated
        logger.debug("core memory capped from %d rows to %d chars", len(rows), len(text))

    _core_cache["text"] = text
    _core_cache["ts"] = now
    _core_cache["count"] = len(rows)

    return text


def get_core_cache_stats() -> dict:
    return {
        "count": _core_cache.get("count", 0),
        "chars": len(_core_cache.get("text", "")),
        "age": round(time.time() - _core_cache.get("ts", 0), 1),
        "max_chars": _CORE_CACHE_MAX_CHARS,
        "ttl": _CORE_CACHE_TTL,
    }


async def search_memory(
    query: str,
    k: int = 6,
    *,
    conversation_id: str | None = None,
    source: str = "retrieval",
) -> list[dict]:
    """Top-k `fact` items for `query` — hybrid dense + BM25, with a real cutoff.

    Returns ``[{id, text, score}]`` where `score` is the dense cosine (0.0 for
    items that surfaced on the lexical arm alone). **May return fewer than `k`,
    or nothing at all**: when no memory clears the relevance cutoff the right
    answer is to inject nothing rather than the k least-irrelevant rows.

    Empty list also when the query is trivial (a greeting) or there are no fact
    rows. Unlike before, a missing embedder is *not* fatal — the lexical arm
    still runs, so keyless / Ollama-less setups keep working memory search.
    Logs access to memory_activities fire-and-forget for audit.
    """
    from core.doc_index import _is_trivial_query, aembed_query_cached

    if _is_trivial_query(query):
        return []

    match_expr = fts_match_expr(query)

    async def _load_rows() -> list:
        async with async_session() as session:
            return await list_memories(session, kind="fact")

    async def _sparse() -> list[str]:
        if match_expr is None:
            return []
        async with async_session() as session:
            return await search_memories_lexical(
                session, match_expr, kind="fact", limit=_SPARSE_CANDIDATES
            )

    # Independent — overlap the embedding round-trip with both DB reads rather
    # than paying them back to back.
    qvec, rows, sparse = await asyncio.gather(
        aembed_query_cached(query, allow_trivial=True), _load_rows(), _sparse()
    )
    if not rows:
        return []

    dense: list[tuple[str, float]] = []
    if qvec is not None:
        qnorm = float(np.linalg.norm(qvec)) or 1.0
        for r in rows:
            if r.embedding is None:
                continue
            score = _cosine(qvec, qnorm, r.embedding)
            if score is not None:
                dense.append((r.id, score))
        dense.sort(key=lambda t: t[1], reverse=True)

    keep = select_hybrid(
        dense=dense,
        sparse=sparse,
        k=k,
        min_score=_MIN_COSINE,
        rel_drop=_REL_DROP,
        label="memory",
    )
    if not keep:
        return []

    dense_scores = dict(dense)
    texts = {r.id: r.text for r in rows}
    result = [
        {"id": i, "text": texts[i], "score": round(dense_scores.get(i, 0.0), 4)}
        for i in keep
        if i in texts
    ]

    # Fire-and-forget activity log — track when memories were used
    if result:
        try:
            from core.memory_activity import touch_memories_background

            # Resolve conversation_id from context if not provided (tool path has it via current_ctx)
            if conversation_id is None:
                try:
                    from tools.context import current_ctx

                    conversation_id = current_ctx().conversation_id
                except Exception:
                    conversation_id = None

            scores_map = {r["id"]: r["score"] for r in result}
            touch_memories_background(
                [r["id"] for r in result],
                scores=scores_map,
                query=query,
                conversation_id=conversation_id,
                kind="fact",
                source=source,
            )
        except Exception:
            pass  # best-effort, never break retrieval

    return result


async def upsert_memory(text: str, kind: str = "fact") -> str | None:
    """Embed `text` and insert it, or merge into a near-duplicate same-kind item.

    Returns the affected memory id, or None when no embedder is available
    (the caller falls back to the blob path).
    """
    text = text.strip()
    if not text:
        return None
    emb = await embed_for_storage(text)
    if emb is None:
        return None
    qvec = np.frombuffer(emb, dtype=np.float32)
    qnorm = float(np.linalg.norm(qvec)) or 1.0

    async with async_session() as session:
        rows = await list_memories(session, kind=kind)
        best_id: str | None = None
        best_score = 0.0
        for r in rows:
            if r.embedding is None:
                continue
            score = _cosine(qvec, qnorm, r.embedding)
            if score is not None and score > best_score:
                best_score = score
                best_id = r.id
        if best_id is not None and best_score >= _DEDUP_THRESHOLD:
            await update_memory_item(session, best_id, text=text, embedding=emb)
            logger.info("memory: merged into %s (cosine=%.3f)", best_id, best_score)
            return best_id
        mem = await create_memory(session, text=text, kind=kind, embedding=emb)
        return mem.id


async def delete_memory_by_id(memory_id: str) -> bool:
    """Delete a Memory row by id. Returns True if deleted."""
    try:
        async with async_session() as session:
            return await delete_memory(session, memory_id)
    except Exception as exc:
        logger.warning("memory delete failed %s: %s", memory_id, exc)
        return False


async def update_memory_with_embedding(memory_id: str, text: str, kind: str = "fact") -> bool:
    """Update text+kind+embedding for an existing memory.

    Used by consolidation's update op — replaces the old text with corrected version.
    """
    text = text.strip()
    if not text:
        return False
    emb = await embed_for_storage(text)
    try:
        async with async_session() as session:
            row = await update_memory_item(
                session, memory_id, text=text, kind=kind, embedding=emb
            )
            return row is not None
    except Exception as exc:
        logger.warning("memory update failed %s: %s", memory_id, exc)
        return False
