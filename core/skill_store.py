"""Skill persistence helpers: embed the description, then CRUD."""

from __future__ import annotations

import logging

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from core.doc_index import embeddings_available, get_embedder
from core.memory_store import embed_for_storage
from db import async_session
from db.models import Skill
from db.ops import create_skill, list_skills, update_skill

logger = logging.getLogger(__name__)

# At or below this many enabled skills, skip retrieval and surface the whole
# catalog — at that size every description fits cheaply in the volatile suffix
# and exact listing beats an embedding round-trip. Above it, narrow to top-K.
_CATALOG_FULL_THRESHOLD = 8
_CATALOG_TOPK = 5


async def embed_description(text: str) -> bytes | None:
    """Embed a skill description for storage/retrieval.

    Returns None — degrading to an unembedded (still fully usable) skill — when
    there is no embedder, the text is empty, or the embedder errors at call time
    (transient 5xx, a misconfigured embedding model id, etc.). A failed embedding
    must never block saving a skill; Phase-2 retrieval falls back to surfacing
    every enabled skill, and the row can be re-embedded later by editing it.
    """
    text = text.strip()
    if not text:
        return None
    try:
        return await embed_for_storage(text)
    except Exception as exc:
        logger.warning("skill description embedding failed; storing unembedded: %s", exc)
        return None


async def save_new_skill(
    session: AsyncSession,
    *,
    name: str,
    description: str,
    body: str,
    enabled: bool = True,
) -> Skill:
    embedding = await embed_description(description)
    return await create_skill(
        session,
        name=name,
        description=description,
        body=body,
        embedding=embedding,
        enabled=enabled,
    )


async def save_skill_update(
    session: AsyncSession,
    skill_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    body: str | None = None,
    enabled: bool | None = None,
) -> Skill | None:
    # Re-embed only when the description (the routing key) actually changes.
    embedding = await embed_description(description) if description is not None else None
    return await update_skill(
        session,
        skill_id,
        name=name,
        description=description,
        body=body,
        enabled=enabled,
        embedding=embedding,
    )


# ── Intent retrieval (Phase 2 surfacing) ──────────────────────────────────────

def _cosine(query_vec: np.ndarray, query_norm: float, stored: bytes) -> float | None:
    """Cosine of `query_vec` (norm precomputed) against stored float32 bytes.

    Returns None when the stored vector was embedded by a different model
    (shape mismatch) — skip rather than crash, mirroring memory_store/doc_index.
    """
    vec = np.frombuffer(stored, dtype=np.float32)
    if vec.shape != query_vec.shape:
        return None
    return float(np.dot(vec, query_vec) / ((float(np.linalg.norm(vec)) or 1.0) * query_norm))


async def search_skills(query: str, *, k: int, skills: list[Skill]) -> list[dict]:
    """Top-k enabled `skills` whose description best matches `query`.

    Returns ``[{name, description}]`` ordered by cosine similarity. Empty when
    there is no embedder, trivial query, or no comparable embedding.
    Uses cached query embedding to share work with memory retrieval.
    """
    from core.doc_index import aembed_query_cached

    if not query.strip():
        return []
    qvec = await aembed_query_cached(query)
    if qvec is None:
        return []
    qnorm = float(np.linalg.norm(qvec)) or 1.0

    scored: list[tuple[float, str, str]] = []
    for s in skills:
        if s.embedding is None:
            continue
        score = _cosine(qvec, qnorm, s.embedding)
        if score is not None:
            scored.append((score, s.name, s.description))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [{"name": n, "description": d} for _, n, d in scored[:k]]


async def skill_catalog(query: str) -> list[dict]:
    """Name + description of the enabled skills to surface this turn.

    Small catalogs (or keyless / no-embedder setups) surface every enabled
    skill; larger ones narrow to the top-K whose descriptions best match
    `query`, falling back to a capped slice if retrieval yields nothing. Bodies
    are never included — the agent pulls those on demand via ``use_skill``.
    Trivial queries (greetings) return empty to avoid wasteful injection.
    """
    from core.doc_index import _is_trivial_query

    if _is_trivial_query(query):
        return []

    async with async_session() as session:
        skills = await list_skills(session, enabled_only=True)
    if not skills:
        return []

    full = [{"name": s.name, "description": s.description} for s in skills]
    if len(skills) <= _CATALOG_FULL_THRESHOLD or not embeddings_available():
        return full

    hits = await search_skills(query, k=_CATALOG_TOPK, skills=skills)
    return hits if hits else full[:_CATALOG_TOPK]
