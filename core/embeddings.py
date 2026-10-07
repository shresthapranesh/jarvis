"""Embeddings: the configured embedder, and a cache of query embeddings
shared by memory, skill and episode retrieval."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "models/gemini-embedding-001"

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
