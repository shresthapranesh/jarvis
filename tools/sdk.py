"""Kernel-side read SDK — preloaded into every run_cell kernel as `jarvis`.

These are plain sync functions the agent calls from Python code, NOT bound
LLM tools: moving the read-only surface (artifacts, indexed documents, board
listing, memory search) out of the tool schemas keeps the per-call prompt
small (see core/agents.py `main_tools`). Write paths (write_artifact,
create_task, remember, …) stay bound tools because they need the server
process: live stream events, scheduler registration, approval interrupts.

The kernel is a separate process, so everything here reads the app database
directly over a read-only sqlite3 connection (`mode=ro` — cannot take write
locks against the server) and reuses `core.doc_index.get_embedder()` for the
two semantic searches. Conversation scope is injected per kernel by
core/kernels.py via `set_conversation()`.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_conversation_id: str | None = None
_embedding_override_applied = False


def set_conversation(conversation_id: str | None) -> None:
    """Scope subsequent calls to a conversation. Called by the kernel bootstrap."""
    global _conversation_id
    _conversation_id = conversation_id


def _db_path() -> str:
    from core.config import get_config

    url = get_config().database_url
    if "sqlite" not in url:
        raise RuntimeError(f"jarvis SDK requires a sqlite database_url, got: {url}")
    return url.rsplit(":///", 1)[-1]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _embedder() -> Any:
    """The app's embeddings client, honoring the `embedding.model` config row."""
    global _embedding_override_applied
    from core.doc_index import configure_embedding_model, get_embedder

    if not _embedding_override_applied:
        _embedding_override_applied = True
        try:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT value FROM config_settings WHERE key = 'embedding.model'"
                ).fetchone()
            if row is not None:
                value = row["value"]
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    pass
                if isinstance(value, str) and value:
                    configure_embedding_model(value)
        except sqlite3.Error:
            pass

    embedder = get_embedder()
    if embedder is None:
        raise RuntimeError("No embedding model available (GOOGLE_API_KEY unset?).")
    return embedder


def _cosine_top_k(qvec: Any, rows: list[tuple[bytes, Any]], k: int) -> list[tuple[float, Any]]:
    """Score (embedding_bytes, payload) rows against qvec, best first."""
    import numpy as np

    q = np.asarray(qvec, dtype=np.float32)
    qnorm = float(np.linalg.norm(q)) or 1.0
    scored: list[tuple[float, Any]] = []
    for blob, payload in rows:
        vec = np.frombuffer(blob, dtype=np.float32)
        if vec.shape != q.shape:  # embedded with a different model — skip
            continue
        score = float(np.dot(vec, q) / ((float(np.linalg.norm(vec)) or 1.0) * qnorm))
        scored.append((score, payload))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored[:k]


# ── Artifacts ─────────────────────────────────────────────────────────────────

def list_artifacts(all_conversations: bool = False) -> list[dict]:
    """Saved artifacts, newest first — current conversation unless all_conversations."""
    sql = (
        "SELECT a.id, a.title, a.conversation_id, a.updated_at,"
        " (SELECT COALESCE(MAX(v.version), 0) FROM artifact_versions v"
        "  WHERE v.artifact_id = a.id) AS versions"
        " FROM artifacts a"
    )
    params: tuple = ()
    if not all_conversations:
        sql += " WHERE a.conversation_id = ?"
        params = (_conversation_id,)
    sql += " ORDER BY a.updated_at DESC"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def read_artifact(artifact_id: str, version: int | None = None) -> str:
    """Markdown content of an artifact — latest, or a specific version."""
    from pathlib import Path

    from core.config import get_config

    artifacts_dir = get_config().artifacts_dir
    with _connect() as conn:
        art = conn.execute(
            "SELECT id FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if art is None:
            raise LookupError(f"Artifact not found: {artifact_id}")
        if version is not None:
            ver = conn.execute(
                "SELECT filename FROM artifact_versions WHERE artifact_id = ? AND version = ?",
                (artifact_id, version),
            ).fetchone()
            if ver is None:
                raise LookupError(f"Artifact version {version} not found for {artifact_id}")
            path = Path(ver["filename"])
            if not path.exists():
                path = artifacts_dir / f"{artifact_id}_v{version}.md"
            if not path.exists():
                raise LookupError(f"Artifact version file missing: {artifact_id} v{version}")
            return path.read_text(encoding="utf-8")
    path = artifacts_dir / f"{artifact_id}.md"
    if not path.exists():
        raise LookupError(f"Artifact file missing on disk: {artifact_id}")
    return path.read_text(encoding="utf-8")


def list_artifact_versions(artifact_id: str) -> list[dict]:
    """Version history for an artifact, oldest first."""
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT version, title, created_at FROM artifact_versions"
                " WHERE artifact_id = ? ORDER BY version",
                (artifact_id,),
            )
        ]


# ── Indexed documents ─────────────────────────────────────────────────────────

_READ_WINDOW_CHARS = 6000


def search_documents(query: str, k: int = 6) -> list[dict]:
    """Top-k passages from this conversation's indexed attachments.

    Phrase `query` as the content you want to find. Follow up with
    read_document(document_id, offset=hit["seq"]) to read around a hit.
    """
    if not _conversation_id:
        raise RuntimeError("No conversation scope — document search is only available in chats.")
    qvec = _embedder().embed_query(query)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT c.embedding, c.document_id, c.seq, c.text, d.filename"
            " FROM document_chunks c JOIN documents d ON c.document_id = d.id"
            " WHERE c.conversation_id = ? AND c.embedding IS NOT NULL",
            (_conversation_id,),
        ).fetchall()
    hits = _cosine_top_k(qvec, [(r["embedding"], r) for r in rows], k)
    return [
        {
            "document_id": r["document_id"],
            "filename": r["filename"],
            "seq": r["seq"],
            "score": round(score, 4),
            "text": r["text"],
        }
        for score, r in hits
    ]


def read_document(document_id: str, offset: int = 0) -> dict:
    """Sequential window of an indexed document; continue with offset=next_offset."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT c.text, d.filename"
            " FROM document_chunks c JOIN documents d ON c.document_id = d.id"
            " WHERE c.document_id = ? ORDER BY c.seq",
            (document_id,),
        ).fetchall()
    if not rows:
        raise LookupError(
            f"Document {document_id} has no index — small attachments are inlined in the message."
        )
    total = len(rows)
    offset = max(0, min(offset, total - 1))
    parts: list[str] = []
    used = 0
    i = offset
    while i < total and used < _READ_WINDOW_CHARS:
        parts.append(rows[i]["text"])
        used += len(rows[i]["text"])
        i += 1
    return {
        "filename": rows[0]["filename"],
        "text": "\n\n".join(parts),
        "offset": offset,
        "next_offset": i if i < total else None,
        "total_chunks": total,
    }


# ── Task board ────────────────────────────────────────────────────────────────

def list_tasks(status: str | None = None) -> list[dict]:
    """Board tasks (durable background work items), highest priority first.

    status: "todo" | "ready" | "running" | "blocked" | "done" | "archived";
    None lists everything except archived.
    """
    sql = (
        "SELECT id, title, status, priority, blocked_reason, blocked_kind,"
        " summary, created_at, updated_at FROM board_tasks"
    )
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    else:
        sql += " WHERE status != 'archived'"
    sql += " ORDER BY priority DESC, created_at"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


# ── Memory ────────────────────────────────────────────────────────────────────

def search_memory(query: str, k: int = 5) -> list[dict]:
    """Top-k long-term memory facts for `query` by cosine similarity."""
    qvec = _embedder().embed_query(query)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, text, embedding FROM memories"
            " WHERE kind = 'fact' AND embedding IS NOT NULL"
        ).fetchall()
    hits = _cosine_top_k(qvec, [(r["embedding"], r) for r in rows], k)
    return [{"id": r["id"], "text": r["text"], "score": round(score, 4)} for score, r in hits]
