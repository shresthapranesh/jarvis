"""Document chunk indexing + semantic search for large attachments.

Small attached documents are inlined into the message as before; documents
over `INLINE_THRESHOLD` characters are chunked, embedded, and stored as
`DocumentChunk` rows instead, with a short stub left in the message telling
the agent to use the `search_documents` / `read_document` tools
(tools/documents.py). This keeps a 300-page PDF out of the per-turn token
bill — and out of the summarizer's jaws — while making all of it queryable.

Embeddings come from a Gemini embedding model (default
`models/gemini-embedding-002`, override with
`config set embedding.model <id>`; requires GOOGLE_API_KEY). When no
embedder is available the caller falls back to inlining (or Ollama
nomic-embed-text if available), so Ollama-only / keyless setups keep
working. Search is brute-force cosine over a conversation's chunks via
numpy — a conversation holds at most a few thousand chunks, where exact
search beats any index.
"""

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

from db import async_session
from db.models import Document, DocumentChunk

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "models/gemini-embedding-002"

# Documents at/below this many extracted chars are inlined into the message
# (retrieval can miss; stuffing can't — only pay its cost when we must).
INLINE_THRESHOLD = 12_000

_CHUNK_SIZE = 1600   # chars per chunk
_CHUNK_OVERLAP = 200
_EMBED_BATCH = 64
_READ_WINDOW_CHARS = 6000

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
    Raises on embedding failure — the caller falls back to inlining.
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
            return existing
        doc = await session.get(Document, document_id)
        if doc is None:
            raise ValueError(f"document {document_id} not found")
        conversation_id = doc.conversation_id

    chunks = chunk_text(text)
    vectors: list[list[float]] = []
    for i in range(0, len(chunks), _EMBED_BATCH):
        batch = chunks[i:i + _EMBED_BATCH]
        vectors.extend(await embedder.aembed_documents(batch))

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
        await session.commit()

    logger.info("indexed document %s: %d chunks (%d chars)", document_id, len(chunks), len(text))
    return len(chunks)


# ── Search / sequential read ─────────────────────────────────────────────────

async def search_chunks(conversation_id: str, query: str, k: int = 6) -> list[dict]:
    """Top-k chunks for `query` across all indexed documents in the
    conversation, by cosine similarity. Returns
    [{document_id, filename, seq, score, text}, ...]."""
    embedder = get_embedder()
    if embedder is None:
        raise RuntimeError("no embedding model available (GOOGLE_API_KEY unset?)")

    # For doc search we allow trivial queries but still use cache
    qvec = await aembed_query_cached(query, allow_trivial=True)
    if qvec is None:
        try:
            qvec = np.asarray(await embedder.aembed_query(query), dtype=np.float32)
        except Exception:
            raise

    async with async_session() as session:
        rows = (await session.execute(
            select(DocumentChunk, Document.filename)
            .join(Document, DocumentChunk.document_id == Document.id)
            .where(
                DocumentChunk.conversation_id == conversation_id,
                DocumentChunk.embedding.is_not(None),
            )
        )).all()

    scored: list[tuple[float, DocumentChunk, str]] = []
    qnorm = float(np.linalg.norm(qvec)) or 1.0
    for chunk, filename in rows:
        vec = np.frombuffer(chunk.embedding, dtype=np.float32)
        if vec.shape != qvec.shape:
            # Chunk was embedded with a different model — skip rather than crash.
            continue
        score = float(np.dot(vec, qvec) / ((float(np.linalg.norm(vec)) or 1.0) * qnorm))
        scored.append((score, chunk, filename))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [
        {
            "document_id": c.document_id,
            "filename": fn,
            "seq": c.seq,
            "score": round(s, 4),
            "text": c.text,
        }
        for s, c, fn in scored[:k]
    ]


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
