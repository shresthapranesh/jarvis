"""Document retrieval tools — search and read large attached documents.

Documents above the inline threshold are chunk-indexed (core/doc_index.py)
instead of being pasted into the message; the message carries a stub with the
document_id. These tools let the agent query that index. Both are scoped to
the current conversation via the same `conversation_id` configurable the
artifact tools use.
"""

from __future__ import annotations

import json
import logging

from langchain_core.tools import tool

from core.doc_index import (
    INDEX_FAILED,
    await_conversation_indexes,
    await_index_ready,
    embeddings_available,
    read_chunks,
    search_chunks,
)
from tools.context import current_ctx

logger = logging.getLogger(__name__)


@tool
async def search_documents(query: str, k: int = 6) -> str:
    """Semantically search the indexed documents attached to this conversation.

    Use this when a message references an attached document that was indexed
    (the attachment stub says so) and you need specific passages — names,
    figures, clauses, sections. Phrase `query` as the content you want to
    find, not a question. Returns the top-k matching passages with their
    document_id and position; follow up with read_document to read around
    a hit.

    Args:
        query: What to look for (descriptive phrase works best).
        k: How many passages to return (default 6).
    """
    conversation_id = current_ctx().conversation_id
    if not conversation_id:
        return "No conversation context — document search is only available in chats."
    if not embeddings_available():
        return "Document search is unavailable (no embedding model configured)."
    # Attachments index in the background so they don't delay the first token.
    # Searching mid-index would return nothing and read as "not in the document",
    # so wait for any still-pending attachment in this conversation first.
    await await_conversation_indexes(conversation_id)
    try:
        hits = await search_chunks(conversation_id, query, k=k)
    except Exception as exc:
        logger.warning("search_documents failed: %s", exc)
        return f"Document search failed: {exc}"
    if not hits:
        return (
            "No matching passages. Either no document in this conversation is "
            "indexed (small attachments are inlined directly in the message), or "
            "nothing cleared the relevance cutoff — try different wording, or "
            "read_document(document_id) to read it directly."
        )
    return json.dumps(hits)


@tool
async def read_document(document_id: str, offset: int = 0) -> str:
    """Read an indexed document sequentially, one window at a time.

    Use this to read a large attached document in order — e.g. to skim from
    the beginning, or to read on from a search_documents hit (pass that hit's
    `seq` as `offset`). Returns a window of text plus `next_offset`; call
    again with `offset=next_offset` to continue. `next_offset` of null means
    you reached the end.

    Args:
        document_id: The id from the attachment stub or a search hit.
        offset: Chunk index to start from (default 0 = start of document).
    """
    status = await await_index_ready(document_id)
    if status == INDEX_FAILED:
        return (
            f"Document {document_id} could not be indexed (the embedding step "
            "failed), so its text isn't available here. Ask the user to re-attach "
            "it, or work from what's already in the conversation."
        )
    try:
        window = await read_chunks(document_id, offset=offset)
    except Exception as exc:
        logger.warning("read_document failed: %s", exc)
        return f"Document read failed: {exc}"
    if window is None:
        return (
            f"Document {document_id} has no index — it was either small enough "
            "to be included directly in the conversation, or the id is wrong."
        )
    return json.dumps(window)
