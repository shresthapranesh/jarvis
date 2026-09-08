"""Attachments reach the agent as a path when code beats reading.

The bug these pin: a ~100KB CSV was extracted to text and pasted into the
prompt (or walked in through read_document windows), because `Document.path`
was written and then never exposed to the agent — so `run_cell` had no way in.
"""

from __future__ import annotations

import base64
import os

import pytest

from core.document_extractor import MAX_CHARS, is_tabular, is_text_tabular
from core.schemas import AttachmentIn
from core.streaming import _build_message_content


def _att(name: str, mime: str, body: bytes, path: str | None, doc_id: str | None = "doc-1"):
    return AttachmentIn(
        type="document", name=name, mime_type=mime,
        data=base64.b64encode(body).decode(), size=len(body),
        document_id=doc_id, document_path=path,
    )


async def _stub(att: AttachmentIn) -> str:
    parts = await _build_message_content("q", [att], "google_genai:x")
    assert isinstance(parts, list)
    return parts[1]["text"]


@pytest.fixture
def csv_file(tmp_path):
    rows = ["id,amount,region"] + [f"{i},{i * 3.5:.2f},r{i % 4}" for i in range(4000)]
    text = "\n".join(rows) + "\n"
    p = tmp_path / "sales.csv"
    p.write_text(text)
    return p, text


async def test_tabular_file_is_not_inlined(csv_file):
    """The file's rows must not reach the prompt — only its path and a head."""
    path, text = csv_file
    stub = await _stub(_att("sales.csv", "text/csv", text.encode(), str(path)))

    assert "3999" not in stub, "file body leaked into the prompt"
    assert str(path) in stub
    assert "4,001 lines" in stub          # measured, not guessed
    assert "id,amount,region" in stub     # header preview
    assert len(stub) < 1000 < len(text)


async def test_tabular_without_a_persisted_file_still_inlines(csv_file):
    """Bots and the CLI write no Document row, so there is no path to hand over."""
    _path, text = csv_file
    stub = await _stub(_att("sales.csv", "text/csv", text.encode(), None, doc_id=None))
    assert "id,amount,region" in stub
    assert "[Document: sales.csv]" in stub


async def test_binary_tabular_is_routed_but_not_previewed(tmp_path):
    """A parquet's first bytes are container framing, not rows."""
    p = tmp_path / "events.parquet"
    p.write_bytes(b"PAR1" + os.urandom(2048))
    stub = await _stub(_att("events.parquet", "application/x-parquet", b"PAR1", str(p)))

    assert str(p) in stub
    assert "run_cell" in stub
    assert "First 5 lines" not in stub


async def test_a_broken_path_still_yields_the_path(tmp_path):
    """A failed preview must not cost the agent the one thing it needs."""
    missing = tmp_path / "gone.csv"
    stub = await _stub(_att("gone.csv", "text/csv", b"a,b\n1,2\n", str(missing)))
    assert str(missing) in stub
    assert "preview unavailable" in stub


async def test_truncated_inline_prose_says_so_and_names_the_file(tmp_path):
    """The silent 80k cut is what let an answer come from the first 78% of a file."""
    prose = "Lorem ipsum dolor sit amet. " * 4000
    p = tmp_path / "memo.txt"
    p.write_text(prose)
    stub = await _stub(_att("memo.txt", "text/plain", prose.encode(), str(p), doc_id=None))

    assert len(prose) > MAX_CHARS
    assert f"Only the first {MAX_CHARS:,}" in stub
    assert str(p) in stub


def test_tabular_detection_covers_mime_and_extension():
    assert is_tabular("text/csv", "x.bin")
    assert is_tabular("application/octet-stream", "x.CSV")
    assert is_tabular("application/octet-stream", "x.parquet")
    assert not is_tabular("application/pdf", "x.pdf")
    assert not is_tabular("text/plain", "notes.txt")

    # Only line-oriented text is previewable by reading the head.
    assert is_text_tabular("text/csv", "x.csv")
    assert not is_text_tabular("application/x-parquet", "x.parquet")
    assert not is_text_tabular("application/octet-stream", "x.xlsx")
