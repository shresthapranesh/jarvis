"""Discrete agent-memory items: vector retrieval + dedup-on-insert.

Replaces the single ``AGENTS.md`` blob with many embedded ``Memory`` rows
(``db/models.Memory``). ``core`` items load on every turn; ``fact`` items are
retrieved by cosine similarity per turn. Reuses the embedder from
``core/doc_index.py`` (Gemini, ``GOOGLE_API_KEY``); when no embedder is
available the whole split system is bypassed and callers fall back to the blob.

Search is brute-force numpy cosine over the (small) global memory set — same
rationale as ``doc_index``: at memory scale exact search beats any index, and
the embedding lives in an ordinary ``LargeBinary`` column, so no vector DB is
needed. Memory text is embedded as a *document* (``aembed_documents``) and the
retrieval query as a *query* (``aembed_query``), matching ``doc_index``.
"""

from __future__ import annotations

import logging

import numpy as np

from core.doc_index import get_embedder
from db import async_session
from db.ops import create_memory, list_memories, update_memory_item

logger = logging.getLogger(__name__)

# Cosine ≥ this against an existing same-kind item ⇒ treat as the same memory
# and update it in place rather than inserting a near-duplicate. Keeps
# contradictions from piling up the way the whole-doc rewrite handled for free.
_DEDUP_THRESHOLD = 0.88


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
    """All `core` memory items, newline-joined. Always injected into context."""
    async with async_session() as session:
        rows = await list_memories(session, kind="core")
    return "\n".join(f"- {r.text}" for r in rows)


async def search_memory(query: str, k: int = 6) -> list[dict]:
    """Top-k `fact` items for `query` by cosine similarity.

    Returns ``[{id, text, score}]``. Empty list when no embedder, no fact rows,
    trivial query (greeting), or every stored embedding mismatches.
    Uses cached query embedding to deduplicate concurrent memory+skill calls.
    """
    from core.doc_index import aembed_query_cached

    qvec = await aembed_query_cached(query)
    if qvec is None:
        return []
    qnorm = float(np.linalg.norm(qvec)) or 1.0

    async with async_session() as session:
        rows = await list_memories(session, kind="fact")

    scored: list[tuple[float, str, str]] = []
    for r in rows:
        if r.embedding is None:
            continue
        score = _cosine(qvec, qnorm, r.embedding)
        if score is not None:
            scored.append((score, r.id, r.text))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [{"id": i, "text": t, "score": round(s, 4)} for s, i, t in scored[:k]]


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
