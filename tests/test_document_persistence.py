"""A chat started with an attachment must leave a real file on disk.

This path shipped 2026-05-20 and, in at least one install, had never once run:
`documents_dir` did not exist, `documents` and `document_chunks` were empty, and
no persist failure was ever logged. Everything downstream — the on-disk path in
the stub, document_id, chunk indexing, the tabular route — is built on the row
this creates, so it is asserted directly rather than assumed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config import get_config
from core.schemas import AttachmentIn
from db import async_session
from db.models import Document
from server.chat_runtime import register_chat_task
from sqlalchemy import select


CSV = b"id,amount,region\n1,10.5,north\n2,20.0,south\n"


async def _documents() -> list[Document]:
    async with async_session() as s:
        return list((await s.execute(select(Document))).scalars())


async def test_attachment_lands_on_disk_with_a_usable_path(jarvis, work_dir: Path):
    att = AttachmentIn(
        type="document", name="expenses.csv", mime_type="text/csv",
        data=__import__("base64").b64encode(CSV).decode(), size=len(CSV),
    )
    async with async_session() as session:
        await register_chat_task(
            session, query="what did I spend?", model="google_genai:x",
            conversation_id=None, attachments=[att],
        )

    docs = await _documents()
    assert len(docs) == 1, "no Document row — the attachment was never persisted"
    doc = docs[0]

    # The directory has to exist, not just be named.
    assert get_config().documents_dir.is_dir()
    # The path on the row must point at the actual bytes: everything the agent
    # does with an attachment now starts by opening this.
    assert Path(doc.path).is_file(), f"row says {doc.path} but nothing is there"
    assert Path(doc.path).read_bytes() == CSV
    assert doc.filename == "expenses.csv"

    # register_chat_task stamps these onto the attachment so the job payload
    # carries them into _build_message_content.
    assert att.document_id == doc.id
    assert att.document_path == doc.path


async def test_the_stub_the_model_sees_names_a_file_that_exists(jarvis, work_dir: Path):
    """The end-to-end contract: path in the prompt == bytes on disk."""
    from core.streaming import _build_message_content

    att = AttachmentIn(
        type="document", name="expenses.csv", mime_type="text/csv",
        data=__import__("base64").b64encode(CSV).decode(), size=len(CSV),
    )
    async with async_session() as session:
        await register_chat_task(
            session, query="total by region?", model="google_genai:x",
            conversation_id=None, attachments=[att],
        )

    parts = await _build_message_content("total by region?", [att], "google_genai:x")
    assert isinstance(parts, list)
    stub = parts[1]["text"]

    assert att.document_path in stub
    assert Path(att.document_path).is_file(), "the stub points the agent at a file that isn't there"


async def test_user_message_records_the_attachment(jarvis, work_dir: Path):
    """The transcript row keeps metadata so a reload still shows the chip."""
    from db.models import Message

    att = AttachmentIn(
        type="document", name="expenses.csv", mime_type="text/csv",
        data=__import__("base64").b64encode(CSV).decode(), size=len(CSV),
    )
    async with async_session() as session:
        await register_chat_task(
            session, query="hi", model="google_genai:x",
            conversation_id=None, attachments=[att],
        )
    async with async_session() as s:
        rows = list((await s.execute(select(Message).where(Message.role == "user"))).scalars())
    parts = json.loads(rows[0].content)
    assert any(p.get("type") == "document" and p.get("name") == "expenses.csv" for p in parts)
