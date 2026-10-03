"""Maintenance over GraphQL — `download-voice`.

The repo path is derived from the voice
*name*, so an unparseable name has to fail loudly rather than 404 halfway
through a 60 MB transfer, and a partial file must never be left where `exists`
would call it done.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest


def _context(session):
    from server.graphql.extensions import SESSION_LOCK_KEY

    return {"session": session, SESSION_LOCK_KEY: asyncio.Lock()}


async def _exec(query: str, variables: dict[str, Any] | None = None) -> Any:
    from db import async_session
    from server.graphql.schema import schema

    async with async_session() as s:
        return await schema.execute(query, variable_values=variables, context_value=_context(s))


# ── Voice ────────────────────────────────────────────────────────────────────

VOICE = "{ voiceStatus { voice directory ready error files { name exists } } }"


async def test_voice_status_reports_missing_without_touching_the_network(database, work_dir):
    res = await _exec(VOICE)
    assert not res.errors, res.errors
    v = res.data["voiceStatus"]
    assert v["ready"] is False
    assert v["error"] == ""
    # Two files: the model and its config. /tts needs both.
    assert [f["name"] for f in v["files"]] == [
        "en_US-hfc_female-medium.onnx",
        "en_US-hfc_female-medium.onnx.json",
    ]


def test_unparseable_voice_name_fails_before_any_request(work_dir):
    from core.voice import voice_status

    s = voice_status("voices/nonsense.onnx", work_dir)
    assert s.ready is False
    assert "cannot parse" in s.error.lower()
    assert s.files == []


def test_ready_when_both_files_are_present(work_dir):
    from core.voice import voice_status

    d = work_dir / "voices"
    d.mkdir(parents=True, exist_ok=True)
    (d / "en_US-hfc_female-medium.onnx").write_bytes(b"model")
    (d / "en_US-hfc_female-medium.onnx.json").write_text("{}")

    s = voice_status("voices/en_US-hfc_female-medium.onnx", work_dir)
    assert s.ready is True
    assert all(f.exists for f in s.files)


def test_a_failed_download_leaves_no_partial_file(work_dir, monkeypatch):
    """`exists` is all the status check can cheaply do, so a truncated .onnx
    would read as ready forever. The download writes to `.part` and renames."""
    import httpx

    from core import voice

    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **kw):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "Client", lambda **kw: _Boom())

    with pytest.raises(httpx.ConnectError):
        voice.download_voice("voices/en_US-hfc_female-medium.onnx", work_dir)

    d = work_dir / "voices"
    assert not (d / "en_US-hfc_female-medium.onnx").exists()
    assert list(d.glob("*.part")) == []
