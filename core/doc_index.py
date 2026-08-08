"""Document chunk indexing + semantic search for large attachments."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from typing import Any
from uuid import uuid4

import numpy as np
from sqlalchemy import func, select

from core.retrieval import env_float
from db import async_session
from db.models import Document, DocumentChunk

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "models/gemini-embedding-001"

# Documents at/below this many extracted chars are inlined into the message
# (retrieval can miss; stuffing can't — only pay its cost when we must).
INLINE_THRESHOLD = 12_000

_CHUNK_SIZE = 1600   # chars per chunk
_CHUNK_OVERLAP = 200
_EMBED_BATCH = 64
# How many embedding batches may be in flight at once while indexing a document.
# Deliberately small: the gain is hiding network latency, not saturating the
# provider, and Gemini's embedding endpoint rate-limits well below what an
# unbounded fan-out would attempt on a large PDF.
_EMBED_CONCURRENCY = int(os.environ.get("JARVIS_EMBED_CONCURRENCY") or 4)
_READ_WINDOW_CHARS = 6000

# Hybrid-retrieval cutoff for document chunks. Looser than the memory cutoff on
# purpose: the agent called `search_documents` explicitly, so a weak passage is
# still more useful than nothing, whereas an unwanted *memory* is injected into
# every prompt without anyone asking. See core/retrieval.py:select_hybrid.
_SPARSE_CANDIDATES = 20
_CHUNK_MIN_COSINE = env_float("JARVIS_DOCS_MIN_COSINE", 0.25)
_CHUNK_REL_DROP = env_float("JARVIS_DOCS_REL_DROP", 0.60)

# ── Query embedding cache (deduplicate + avoid re-embedding same text) ────────
# Many turns reuse similar queries; memory + skills both embed the same query
# concurrently. Cache stores numpy vectors keyed by "model::normalized_query"
# with 1h TTL and 512-entry LRU. Concurrent callers for same key share one Task.
_QUERY_CACHE_MAX = 512
_QUERY_CACHE_TTL = 3600  # seconds
_query_cache: OrderedDict[str, tuple[np.ndarray, float]] = OrderedDict()
_query_tasks: dict[str, asyncio.Task[np.ndarray | None]] = {}

# Metrics for /server-logs observability
_query_cache_metrics: dict[str, int] = {
    "hits": 0,
    "misses": 0,
    "trivial": 0,
    "dedup": 0,
    "total": 0,
    "errors": 0,
}

# Process-wide override, set from the `embedding.model` config row by the
# server lifespan.
_embedding_model_override: str | None = None
_embedder_cache: dict[str, Any] = {}


def configure_embedding_model(model_id: str | None) -> None:
    """Override the embedding model. Pass None to use the default."""
    global _embedding_model_override
    _embedding_model_override = model_id or None


def _effective_model() -> str:
    model = _embedding_model_override or DEFAULT_EMBEDDING_MODEL
    # langchain-google-genai expects the "models/" prefix.
    return model if model.startswith("models/") else f"models/{model}"


def get_embedder() -> Any | None:
    """Return a cached embeddings client, or None when unavailable.

    Priority: Google Gemini if GOOGLE_API_KEY set, else Ollama (nomic-embed-text)
    if langchain-ollama is installed. Callers treat None as 'fall back to
    inlining / no memory search'. Cache keyed by effective model.
    """
    model = _effective_model()
    cached = _embedder_cache.get(model)
    if cached is not None:
        return cached

    # Google path
    if os.environ.get("GOOGLE_API_KEY"):
        try:
            from langchain_google_genai import GoogleGenerativeAIEmbeddings  # noqa: PLC0415

            embedder = GoogleGenerativeAIEmbeddings(model=model)
            _embedder_cache[model] = embedder
            return embedder
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("Google embedder init failed: %s", exc)

    # Ollama fallback — lets Ollama-only setups still have vector memory
    try:
        from langchain_ollama import OllamaEmbeddings  # noqa: PLC0415

        # If user overrode embedding.model with a non-Google name, use that,
        # else default to nomic-embed-text (common Ollama embedding model)
        ollama_model = _embedding_model_override if _embedding_model_override else "nomic-embed-text"
        # Strip Google prefix if user accidentally left it
        if ollama_model.startswith("models/"):
            ollama_model = "nomic-embed-text"

        ollama_key = f"ollama::{ollama_model}"
        cached_ollama = _embedder_cache.get(ollama_key)
        if cached_ollama is not None:
            return cached_ollama

        embedder = OllamaEmbeddings(model=ollama_model)
        _embedder_cache[ollama_key] = embedder
        _embedder_cache[model] = embedder  # also cache under requested model for quick lookup
        logger.info("Using Ollama embeddings fallback model=%s", ollama_model)
        return embedder
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("Ollama embedder not available: %s", exc)

    return None


def embeddings_available() -> bool:
    return get_embedder() is not None


# ── Query embedding cache with deduplication ──────────────────────────────────

_TRIVIAL_QUERIES = {
    "hi", "hello", "hey", "thanks", "thank you", "ty", "ok", "okay", "yes", "no", "sure",
    "hello there", "hi there", "hey there", "thanks!", "thank you!", "ok thanks",
}


def _is_trivial_query(query: str) -> bool:
    """Heuristic: greetings / very short small-talk don't need fact retrieval."""
    q = query.strip().lower()
    if not q:
        return True
    if q in _TRIVIAL_QUERIES:
        return True
    if len(q) <= 4:
        return True
    # "hi" + punctuation already covered, but also "yo", "sup" etc short
    return False


async def _aembed_query_inner(query: str) -> np.ndarray | None:
    embedder = get_embedder()
    if embedder is None:
        return None
    try:
        vec = await embedder.aembed_query(query)
        return np.asarray(vec, dtype=np.float32)
    except Exception as exc:
        logger.warning("query embedding failed: %s", exc)
        _query_cache_metrics["errors"] += 1
        return None


async def aembed_query_cached(query: str, *, allow_trivial: bool = False) -> np.ndarray | None:
    """Cached, deduplicated query embedding. Returns None if no embedder or on error.

    Keyed by "model::normalized_query". Concurrent callers for same key share
    one Task. LRU with TTL. Trivial queries bypass embedding (return None) unless
    allow_trivial=True — callers treat None as 'skip fact retrieval, only core memories'.
    Metrics are emitted via logger.debug for /server-logs observability.
    """
    _query_cache_metrics["total"] += 1

    if not allow_trivial and _is_trivial_query(query):
        _query_cache_metrics["trivial"] += 1
        logger.debug("query-cache trivial skip query=%r", query[:80])
        return None

    normalized = " ".join(query.strip().split())  # collapse whitespace
    if len(normalized) < 3:
        if not allow_trivial:
            _query_cache_metrics["trivial"] += 1
            logger.debug("query-cache too short skip query=%r", query[:80])
            return None

    model = _effective_model()
    cache_key = f"{model}::{normalized}"

    now = time.time()

    # Fast path: cache hit and not expired
    cached = _query_cache.get(cache_key)
    if cached is not None:
        vec, ts = cached
        if now - ts < _QUERY_CACHE_TTL:
            _query_cache.move_to_end(cache_key)
            _query_cache_metrics["hits"] += 1
            logger.debug(
                "query-cache hit key=%s hit_rate=%.2f size=%d",
                cache_key[:120],
                _query_cache_metrics["hits"] / max(1, _query_cache_metrics["total"]),
                len(_query_cache),
            )
            return vec
        else:
            _query_cache.pop(cache_key, None)

    # Deduplicate concurrent in-flight embeddings for same key
    existing_task = _query_tasks.get(cache_key)
    if existing_task is not None:
        _query_cache_metrics["dedup"] += 1
        logger.debug("query-cache dedup inflight key=%s", cache_key[:120])
        try:
            return await existing_task
        except Exception:
            _query_tasks.pop(cache_key, None)

    # Cache miss — create new task
    _query_cache_metrics["misses"] += 1
    logger.debug(
        "query-cache miss key=%s misses=%d total=%d",
        cache_key[:120],
        _query_cache_metrics["misses"],
        _query_cache_metrics["total"],
    )

    task = asyncio.create_task(_aembed_query_inner(query))
    _query_tasks[cache_key] = task

    try:
        vec = await task
        if vec is not None:
            _query_cache[cache_key] = (vec, now)
            while len(_query_cache) > _QUERY_CACHE_MAX:
                _query_cache.popitem(last=False)
            # Periodic info log every 50 misses or 100 total for /server-logs visibility
            total = _query_cache_metrics["total"]
            if total % 20 == 0 or _query_cache_metrics["misses"] % 10 == 0:
                logger.info(
                    "query-cache stats hits=%d misses=%d dedup=%d trivial=%d total=%d hit_rate=%.1f%% size=%d",
                    _query_cache_metrics["hits"],
                    _query_cache_metrics["misses"],
                    _query_cache_metrics["dedup"],
                    _query_cache_metrics["trivial"],
                    total,
                    100.0 * _query_cache_metrics["hits"] / max(1, total),
                    len(_query_cache),
                )
        return vec
    finally:
        _query_tasks.pop(cache_key, None)


def get_query_cache_stats() -> dict:
    total = _query_cache_metrics["total"]
    hits = _query_cache_metrics["hits"]
    return {
        "size": len(_query_cache),
        "inflight": len(_query_tasks),
        "max": _QUERY_CACHE_MAX,
        "ttl": _QUERY_CACHE_TTL,
        "hits": hits,
        "misses": _query_cache_metrics["misses"],
        "dedup": _query_cache_metrics["dedup"],
        "trivial": _query_cache_metrics["trivial"],
        "total": total,
        "errors": _query_cache_metrics["errors"],
        "hit_rate": round(hits / max(1, total), 3),
        "saved_calls": hits + _query_cache_metrics["dedup"] + _query_cache_metrics["trivial"],
    }


def log_query_cache_stats() -> None:
    s = get_query_cache_stats()
    logger.info(
        "query-cache final stats hits=%d misses=%d dedup=%d trivial=%d total=%d hit_rate=%.1f%% saved=%d size=%d",
        s["hits"],
        s["misses"],
        s["dedup"],
        s["trivial"],
        s["total"],
        s["hit_rate"] * 100,
        s["saved_calls"],
        s["size"],
    )


# ── Chunking ─────────────────────────────────────────────────────────────────

def chunk_text(text: str, *, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Split text into ~`size`-char chunks with `overlap`, preferring to break
    at a paragraph or sentence boundary near the end of each window."""
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # Look for a natural break in the last 20% of the window.
            window = text[start:end]
            floor = int(size * 0.8)
            cut = max(window.rfind("\n\n", floor), window.rfind(". ", floor))
            if cut > 0:
                end = start + cut + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


# ── Indexing ─────────────────────────────────────────────────────────────────

async def index_document(document_id: str, text: str) -> int:
    """Chunk + embed `text` and store DocumentChunk rows for `document_id`.

    Idempotent: if the document already has chunks (e.g. a chat job retried
    after a restart), returns the existing count without re-embedding.
    Raises on embedding failure; `start_indexing` records that as
    `index_status='failed'` so the retrieval tools can say so.
    """
    embedder = get_embedder()
    if embedder is None:
        raise RuntimeError("no embedding model available (GOOGLE_API_KEY unset?)")

    async with async_session() as session:
        existing = (await session.execute(
            select(func.count()).select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
        )).scalar_one()
        if existing:
            await _set_index_status(document_id, INDEX_INDEXED, session=session)
            return existing
        doc = await session.get(Document, document_id)
        if doc is None:
            raise ValueError(f"document {document_id} not found")
        conversation_id = doc.conversation_id

    chunks = chunk_text(text)
    batches = [chunks[i:i + _EMBED_BATCH] for i in range(0, len(chunks), _EMBED_BATCH)]

    # Batches are network-bound and independent, so run a few at a time instead
    # of strictly back to back. Bounded by a semaphore rather than unleashed —
    # a 200-page PDF is dozens of batches and providers rate-limit.
    sem = asyncio.Semaphore(_EMBED_CONCURRENCY)

    async def _embed(batch: list[str]) -> list[list[float]]:
        async with sem:
            return await embedder.aembed_documents(batch)

    # gather preserves input order, so vectors still line up with chunk seq.
    results = await asyncio.gather(*(_embed(b) for b in batches))
    vectors: list[list[float]] = [vec for batch_vecs in results for vec in batch_vecs]

    async with async_session() as session:
        for seq, (chunk, vec) in enumerate(zip(chunks, vectors)):
            session.add(DocumentChunk(
                id=str(uuid4()),
                document_id=document_id,
                conversation_id=conversation_id,
                seq=seq,
                text=chunk,
                embedding=np.asarray(vec, dtype=np.float32).tobytes(),
            ))
        # Chunks and the 'indexed' flag land in one transaction, so a reader can
        # never see the flag before the rows it promises.
        await _set_index_status(document_id, INDEX_INDEXED, session=session)

    logger.info("indexed document %s: %d chunks (%d chars)", document_id, len(chunks), len(text))
    return len(chunks)


# ── Background indexing + readiness ──────────────────────────────────────────
# Indexing a large document costs seconds of embedding round-trips. Doing it
# inline before the agent starts put all of that in front of the first token,
# for work the agent often doesn't need until several turns later (if at all).
#
# So it runs in the background — but fire-and-forget would be wrong: the stub
# tells the agent to call search_documents, and an empty result is a *valid*
# outcome in this retrieval design (see core/retrieval.select_hybrid). A search
# racing the indexer would return nothing and the agent would reasonably
# conclude the document has nothing relevant. Hence a readiness signal, in two
# layers because the `jarvis` SDK runs inside a separate kernel process:
#   - in-process (bound tools): await the actual task
#   - cross-process (kernel SDK): poll Document.index_status

INDEX_PENDING = "pending"
INDEX_INDEXED = "indexed"
INDEX_FAILED = "failed"

# How long a reader waits for an in-flight index before giving up and querying
# whatever is there. Generous: the alternative to waiting is a confidently wrong
# "nothing in this document" answer.
INDEX_WAIT_TIMEOUT = 120.0

_indexing_tasks: dict[str, asyncio.Task[int]] = {}


async def _set_index_status(document_id: str, status: str, *, session=None) -> None:
    """Write Document.index_status, committing on the given or a new session."""
    async def _apply(s) -> None:
        doc = await s.get(Document, document_id)
        if doc is not None:
            doc.index_status = status
        await s.commit()

    try:
        if session is not None:
            await _apply(session)
        else:
            async with async_session() as s:
                await _apply(s)
    except Exception as exc:
        logger.warning("could not set index_status=%s for %s: %s", status, document_id, exc)


def start_indexing(document_id: str, text: str) -> asyncio.Task[int]:
    """Kick off indexing in the background and return its task.

    Marks the document `pending` before returning so a reader in another process
    knows to wait. The task marks `indexed` or `failed` when it settles.
    """
    existing = _indexing_tasks.get(document_id)
    if existing is not None and not existing.done():
        return existing

    async def _run() -> int:
        await _set_index_status(document_id, INDEX_PENDING)
        try:
            return await index_document(document_id, text)
        except Exception as exc:
            # Unlike the old inline path we can't fall back to inlining — the
            # message content is already fixed by the time this runs — so record
            # the failure for the retrieval tools to report.
            logger.warning("background indexing of %s failed: %s", document_id, exc)
            await _set_index_status(document_id, INDEX_FAILED)
            raise
        finally:
            _indexing_tasks.pop(document_id, None)

    task = asyncio.create_task(_run())
    # Consume the exception so a failed index that nobody awaited doesn't surface
    # as asyncio's "Task exception was never retrieved" at GC. Callers that do
    # await still see it; the durable signal is index_status='failed'.
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    _indexing_tasks[document_id] = task
    return task


async def _read_index_status(document_id: str) -> str | None:
    async with async_session() as session:
        doc = await session.get(Document, document_id)
        return None if doc is None else doc.index_status


async def await_index_ready(
    document_id: str, *, timeout: float = INDEX_WAIT_TIMEOUT
) -> str | None:
    """Block until `document_id` is done indexing; return its final status.

    Awaits the in-process task when this process owns it, otherwise polls the
    DB flag. Returns the status ('indexed' / 'failed' / None for never-indexed),
    or the last-seen status if `timeout` elapses first.
    """
    task = _indexing_tasks.get(document_id)
    if task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("timed out waiting for index of %s", document_id)
        except Exception:
            pass  # the task already recorded 'failed'
        return await _read_index_status(document_id)

    status = await _read_index_status(document_id)
    if status != INDEX_PENDING:
        return status

    # Indexing belongs to another process (or an earlier run of this one) —
    # poll the flag it writes.
    deadline = time.monotonic() + timeout
    delay = 0.1
    while time.monotonic() < deadline:
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 2.0)
        status = await _read_index_status(document_id)
        if status != INDEX_PENDING:
            return status
    logger.warning("timed out waiting for index of %s (cross-process)", document_id)
    return status


async def await_conversation_indexes(
    conversation_id: str, *, timeout: float = INDEX_WAIT_TIMEOUT
) -> None:
    """Wait for every still-pending document in a conversation.

    `search_documents` is conversation-scoped, so it has to wait on all of them,
    not just one — a hit could live in any attachment.

    Selects every document in the conversation, not just the ones already
    flagged `pending`: `start_indexing` registers its task synchronously but
    writes the flag from inside that task, so a DB-only filter can miss a
    just-started index and let the search run against an empty table.
    """
    async with async_session() as session:
        rows = list((await session.execute(
            select(Document.id, Document.index_status).where(
                Document.conversation_id == conversation_id
            )
        )).all())
    pending = [
        doc_id for doc_id, status in rows
        if status == INDEX_PENDING or doc_id in _indexing_tasks
    ]
    if not pending:
        return
    await asyncio.gather(
        *(await_index_ready(doc_id, timeout=timeout) for doc_id in pending)
    )


# ── Search / sequential read ─────────────────────────────────────────────────

async def search_chunks(conversation_id: str, query: str, k: int = 6) -> list[dict]:
    """Top-k chunks for `query` across all indexed documents in the conversation.

    Hybrid: dense cosine + BM25 over the FTS5 mirror, fused by rank. The lexical
    arm is what makes exact-token lookups work — an error code, a config key, an
    identifier has no *meaning* for an embedding to encode, so dense search
    scores it as unremarkable while BM25 lands on it directly.

    Returns [{document_id, filename, seq, score, text}, ...] — possibly fewer
    than `k`, or empty when nothing clears the cutoff. Raises only when neither
    arm can run (no embedder *and* no usable lexical terms).
    """
    from core.retrieval import cosine_ranking, fts_match_expr, select_hybrid
    from db.ops import search_chunks_lexical

    embedder = get_embedder()
    match_expr = fts_match_expr(query)
    if embedder is None and match_expr is None:
        raise RuntimeError("no embedding model available (GOOGLE_API_KEY unset?)")

    async def _load_rows() -> list[Any]:
        async with async_session() as session:
            return list((await session.execute(
                select(DocumentChunk, Document.filename)
                .join(Document, DocumentChunk.document_id == Document.id)
                .where(DocumentChunk.conversation_id == conversation_id)
            )).all())

    async def _sparse() -> list[str]:
        if match_expr is None:
            return []
        async with async_session() as session:
            return await search_chunks_lexical(
                session, conversation_id, match_expr, limit=_SPARSE_CANDIDATES
            )

    # Doc search allows trivial queries (the agent asked explicitly) but still
    # goes through the shared query-embedding cache.
    qvec, rows, sparse = await asyncio.gather(
        aembed_query_cached(query, allow_trivial=True), _load_rows(), _sparse()
    )
    if not rows:
        return []

    by_id: dict[str, tuple[DocumentChunk, str]] = {c.id: (c, fn) for c, fn in rows}

    dense: list[tuple[str, float]] = []
    if qvec is not None:
        # Chunks embedded by a different model are skipped inside cosine_ranking
        # (shape mismatch) rather than crashing the search.
        dense = cosine_ranking(qvec, [(chunk.id, chunk.embedding) for chunk, _fn in rows])

    keep = select_hybrid(
        dense=dense,
        sparse=sparse,
        k=k,
        min_score=_CHUNK_MIN_COSINE,
        rel_drop=_CHUNK_REL_DROP,
        label="documents",
    )

    dense_scores = dict(dense)
    out: list[dict] = []
    for chunk_id in keep:
        entry = by_id.get(chunk_id)
        if entry is None:
            continue
        chunk, filename = entry
        out.append({
            "document_id": chunk.document_id,
            "filename": filename,
            "seq": chunk.seq,
            "score": round(dense_scores.get(chunk_id, 0.0), 4),
            "text": chunk.text,
        })
    return out


async def read_chunks(document_id: str, offset: int = 0) -> dict | None:
    """Sequential read: join chunks from `offset` up to ~_READ_WINDOW_CHARS.

    Returns {filename, text, offset, next_offset, total_chunks} — next_offset
    is None at the end of the document. Returns None if the document has no
    chunks (not indexed, or unknown id)."""
    async with async_session() as session:
        rows = (await session.execute(
            select(DocumentChunk, Document.filename)
            .join(Document, DocumentChunk.document_id == Document.id)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.seq)
        )).all()
    if not rows:
        return None

    total = len(rows)
    offset = max(0, min(offset, total - 1))
    parts: list[str] = []
    used = 0
    i = offset
    while i < total and used < _READ_WINDOW_CHARS:
        text = rows[i][0].text
        parts.append(text)
        used += len(text)
        i += 1

    return {
        "filename": rows[0][1],
        "text": "\n\n".join(parts),
        "offset": offset,
        "next_offset": i if i < total else None,
        "total_chunks": total,
    }
