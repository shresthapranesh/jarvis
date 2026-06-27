"""Document chunk indexing + semantic search for large attachments.

Small attached documents are inlined into the message as before; documents
over `INLINE_THRESHOLD` characters are chunked, embedded, and stored as
`DocumentChunk` rows instead, with a short stub left in the message telling
the agent to use the `search_documents` / `read_document` tools
(tools/documents.py). This keeps a 300-page PDF out of the per-turn token
bill — and out of the summarizer's jaws — while making all of it queryable.

Embeddings come from a Gemini embedding model (default
`models/gemini-embedding-001`, override with
`config set embedding.model <id>`; requires GOOGLE_API_KEY). When no
embedder is available the caller falls back to inlining, so Ollama-only /
keyless setups keep today's behavior. Search is brute-force cosine over a
conversation's chunks via numpy — a conversation holds at most a few
thousand chunks, where exact search beats any index.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from uuid import uuid4

import numpy as np
from sqlalchemy import func, select

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
_READ_WINDOW_CHARS = 6000

# Process-wide override, set from the `embedding.model` config row by the
# server lifespan (same pattern as safety.configure_judge_model).
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
    """Return a cached embeddings client, or None when unavailable
    (no GOOGLE_API_KEY / package missing). Callers treat None as
    "fall back to inlining"."""
    if not os.environ.get("GOOGLE_API_KEY"):
        return None
    model = _effective_model()
    cached = _embedder_cache.get(model)
    if cached is not None:
        return cached
    try:
        from langchain_google_genai import GoogleGenerativeAIEmbeddings  # noqa: PLC0415
    except ImportError:
        return None
    embedder = GoogleGenerativeAIEmbeddings(model=model)
    _embedder_cache[model] = embedder
    return embedder


def embeddings_available() -> bool:
    return get_embedder() is not None


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

    qvec = np.asarray(await embedder.aembed_query(query), dtype=np.float32)

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
