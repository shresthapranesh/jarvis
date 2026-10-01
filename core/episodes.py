"""Conversation episodes: compacted stretches of one thread, kept retrievable.

Compaction (core/compaction.py) summarizes the chunk it evicts and merges that
into one running summary capped at ~800 words. That is the right shape for "what
has this conversation been about", and the wrong one for a thread that never
ends: every merge re-compresses everything before it, so a decision from three
weeks ago in a Telegram chat survives only as long as the summarizer keeps
choosing to mention it.

So the per-chunk summary is also stored here, embedded, and on later turns the
episodes relevant to the current message are retrieved into the uncached tail.
The running summary stays the always-on overview; episodes are the detail
behind it, fetched only when the conversation comes back to them.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from core.retrieval import cosine_ranking, env_float, fts_match_expr, select_hybrid
from db import async_session
from db.models import Conversation
from db.ops import create_episode, list_episodes, search_episodes_lexical

logger = logging.getLogger(__name__)

# Same calibration story as core/memory_store.py: the floor is model-specific and
# can only be tuned against the scores select_hybrid logs.
_MIN_COSINE = env_float("JARVIS_EPISODE_MIN_COSINE", 0.30)
_REL_DROP = env_float("JARVIS_EPISODE_REL_DROP", 0.75)
_SPARSE_CANDIDATES = 20

# Retrieved episodes ride in the uncached tail, so they are re-billed on every
# LLM call of the turn. A few relevant ones are the point; a long tail is not.
EPISODES_PER_TURN = 3
_EPISODE_INJECT_CHARS = 2_000


def episode_id(conversation_id: str, evicted_ids: list[str]) -> str:
    """Stable id for the episode covering `evicted_ids` in `conversation_id`."""
    digest = hashlib.sha256("\x1f".join([conversation_id, *evicted_ids]).encode()).hexdigest()
    return f"ep_{digest[:32]}"


async def record_episode(conversation_id: str, text: str, evicted_ids: list[str]) -> bool:
    """Store one compacted chunk's summary. Returns True if a row was written.

    Embedded when an embedder is configured; keyless installs still get the
    lexical arm. A thread with no Conversation row (a stateless automation run's
    per-run thread) is skipped — nothing would ever delete the episode.
    """
    from core.memory_store import embed_for_storage

    text = text.strip()
    if not conversation_id or not text:
        return False
    async with async_session() as session:
        if await session.get(Conversation, conversation_id) is None:
            return False
    embedding = await embed_for_storage(text)
    async with async_session() as session:
        written = await create_episode(
            session,
            episode_id=episode_id(conversation_id, evicted_ids),
            conversation_id=conversation_id,
            text=text,
            embedding=embedding,
        )
    if written:
        logger.info("episode recorded for %s (%d chars)", conversation_id, len(text))
    return written


async def search_episodes(conversation_id: str, query: str, k: int = EPISODES_PER_TURN) -> list[dict]:
    """Episodes of `conversation_id` relevant to `query`, oldest first.

    Returns ``[{id, text, created_at}]``. Ranked with the same hybrid dense +
    BM25 cutoff as memory, so it **may return nothing** — most turns aren't
    about anything compacted away, and then nothing is injected. Results are
    re-sorted chronologically because they are read as history.
    """
    from core.doc_index import _is_trivial_query, aembed_query_cached

    if not conversation_id or _is_trivial_query(query):
        return []

    async with async_session() as session:
        rows = await list_episodes(session, conversation_id)
    if not rows:
        return []

    match_expr = fts_match_expr(query)

    async def _sparse() -> list[str]:
        if match_expr is None:
            return []
        async with async_session() as session:
            return await search_episodes_lexical(
                session, conversation_id, match_expr, limit=_SPARSE_CANDIDATES
            )

    qvec, sparse = await asyncio.gather(aembed_query_cached(query, allow_trivial=True), _sparse())
    dense = cosine_ranking(qvec, [(r.id, r.embedding) for r in rows]) if qvec is not None else []
    if dense:
        # Unlike memory items, episodes are all written by one summarizer in
        # one register — "decided", "discussed", "the user" appear in nearly
        # every one — so a shared word is weak evidence here, and with only a
        # handful of episodes per thread the top-k lexical bypass would let
        # almost any query through. Where cosine can score a row it decides;
        # the lexical arm only covers rows it can't (stored before an embedder
        # was configured).
        scored = {item_id for item_id, _ in dense}
        sparse = [i for i in sparse if i not in scored]
    keep = set(
        select_hybrid(
            dense=dense, sparse=sparse, k=k, min_score=_MIN_COSINE, rel_drop=_REL_DROP,
            label="episode",
        )[:k]
    )
    return [
        {"id": r.id, "text": r.text, "created_at": r.created_at}
        for r in rows
        if r.id in keep
    ]


def render_episodes(episodes: list[dict]) -> str:
    """The `## Earlier in this conversation` section for the volatile tail."""
    blocks = []
    for e in episodes:
        body = e["text"].strip()
        if len(body) > _EPISODE_INJECT_CHARS:
            body = body[:_EPISODE_INJECT_CHARS].rsplit(" ", 1)[0] + " …"
        blocks.append(f"### Summarized {e['created_at']:%Y-%m-%d %H:%M}\n{body}")
    return (
        "## Earlier in this conversation\n\n"
        "Summaries of stretches of this conversation that were compacted out of "
        "context, retrieved because they look relevant to the current message. "
        "For the exact wording, `jarvis.read_conversation(limit=...)` in run_cell "
        "returns the original messages.\n\n" + "\n\n".join(blocks)
    )
