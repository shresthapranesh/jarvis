"""Model discovery — the drift diff, the chat heuristic, and the probe's
argument handling.

Everything here is offline: the Google adapter is driven through a stubbed
httpx client, so the tests assert how the response is *parsed* rather than what
the provider happens to serve today.
"""

from __future__ import annotations

import json

import pytest

from core.model_catalog import ModelSpec
from core.model_discovery import (
    DiscoveredModel,
    DiscoveryError,
    _is_signature_error,
    build_report,
    discover,
    looks_like_chat,
    probe,
)


# ── The chat heuristic ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "gemini-3.5-flash", "gemini-2.5-pro", "gemma-4-31b-it", "gemini-flash-latest",
])
def test_looks_like_chat_accepts_text_models(name: str) -> None:
    assert looks_like_chat(name)


@pytest.mark.parametrize("name", [
    "gemini-2.5-flash-preview-tts",   # speech
    "gemini-3.1-flash-image",         # image
    "lyria-3-pro-preview",            # music
    "nano-banana-pro-preview",        # image, branded name
    "gemini-robotics-er-2-preview",   # not a chat surface
    "text-embedding-004",
])
def test_looks_like_chat_rejects_non_text_models(name: str) -> None:
    assert not looks_like_chat(name)


# ── Drift report ─────────────────────────────────────────────────────────────

def _live(mid: str, window: int | None = None) -> DiscoveredModel:
    return DiscoveredModel(id=mid, label=mid, provider="google_genai", context_window=window)


def test_report_flags_catalog_entry_the_provider_dropped(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.model_catalog.available_models",
        lambda: [ModelSpec("google_genai:retired", "Retired", "google_genai")],
    )
    report = build_report("google_genai", [_live("google_genai:current")])
    assert report.missing == ["google_genai:retired"]
    assert [m.id for m in report.new] == ["google_genai:current"]
    assert not report.clean


def test_report_offers_context_window_when_catalog_has_none(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.model_catalog.available_models",
        lambda: [ModelSpec("google_genai:m", "M", "google_genai", None)],
    )
    report = build_report("google_genai", [_live("google_genai:m", 262_144)])
    assert report.window_backfill == [("google_genai:m", 262_144)]
    assert report.window_drift == []


def test_report_flags_context_window_disagreement(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.model_catalog.available_models",
        lambda: [ModelSpec("google_genai:m", "M", "google_genai", 1_000)],
    )
    report = build_report("google_genai", [_live("google_genai:m", 2_000)])
    assert report.window_drift == [("google_genai:m", 1_000, 2_000)]
    assert report.window_backfill == []


def test_report_is_clean_when_catalog_matches(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.model_catalog.available_models",
        lambda: [ModelSpec("google_genai:m", "M", "google_genai", 2_000)],
    )
    assert build_report("google_genai", [_live("google_genai:m", 2_000)]).clean


def test_report_ignores_other_providers(monkeypatch) -> None:
    """A google sync must not report every ollama model as missing."""
    monkeypatch.setattr(
        "core.model_catalog.available_models",
        lambda: [
            ModelSpec("google_genai:m", "M", "google_genai"),
            ModelSpec("ollama:llama3", "Llama", "ollama"),
        ],
    )
    assert build_report("google_genai", [_live("google_genai:m")]).missing == []


# ── Google adapter parsing ───────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Serves a canned page sequence, so pagination is exercised for real."""

    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages
        self.calls: list[dict] = []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def get(self, url, headers=None, params=None):
        self.calls.append(dict(params or {}))
        return _FakeResponse(self._pages[len(self.calls) - 1])


def _install(monkeypatch, pages: list[dict]) -> _FakeClient:
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    client = _FakeClient(pages)
    monkeypatch.setattr("httpx.Client", lambda **kw: client)
    return client


def test_google_keeps_only_generate_content_models(monkeypatch) -> None:
    _install(monkeypatch, [{"models": [
        {"name": "models/chat-one", "displayName": "Chat One",
         "inputTokenLimit": 1024, "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/embed-one", "displayName": "Embed One",
         "supportedGenerationMethods": ["embedContent"]},
    ]}])
    found = discover("google_genai")
    assert [m.id for m in found] == ["google_genai:chat-one"]
    assert found[0].label == "Chat One"
    assert found[0].context_window == 1024


def test_google_marks_non_chat_models_without_dropping_them(monkeypatch) -> None:
    """Non-text generators must stay visible — flagged, never silently filtered."""
    _install(monkeypatch, [{"models": [
        {"name": "models/gemini-9-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/lyria-9-preview", "supportedGenerationMethods": ["generateContent"]},
    ]}])
    found = {m.id: m for m in discover("google_genai")}
    assert len(found) == 2
    assert found["google_genai:gemini-9-flash"].likely_chat
    assert not found["google_genai:lyria-9-preview"].likely_chat


def test_google_follows_pagination(monkeypatch) -> None:
    client = _install(monkeypatch, [
        {"models": [{"name": "models/a", "supportedGenerationMethods": ["generateContent"]}],
         "nextPageToken": "tok"},
        {"models": [{"name": "models/b", "supportedGenerationMethods": ["generateContent"]}]},
    ])
    assert [m.id for m in discover("google_genai")] == ["google_genai:a", "google_genai:b"]
    assert client.calls[1]["pageToken"] == "tok"


def test_google_without_key_raises_rather_than_returning_empty(monkeypatch) -> None:
    """An empty list means 'the provider offers nothing' — never 'we couldn't ask'."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(DiscoveryError, match="GOOGLE_API_KEY"):
        discover("google_genai")


def test_unknown_provider_raises(monkeypatch) -> None:
    with pytest.raises(DiscoveryError, match="not implemented"):
        discover("nope")


# ── Probe ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("exc", [
    TypeError("unexpected keyword argument 'max_tokens'"),
    ValueError("1 validation error for GenerateContentConfig\nmax_tokens\n  Extra inputs are not permitted"),
])
def test_signature_errors_are_recognised(exc: Exception) -> None:
    assert _is_signature_error(exc)


def test_provider_errors_are_not_signature_errors() -> None:
    assert not _is_signature_error(RuntimeError("404 NOT_FOUND: model no longer available"))


def test_probe_retries_uncapped_when_the_cap_is_rejected(monkeypatch) -> None:
    """A provider that rejects `max_tokens` must not read as unreachable."""
    seen: list[dict] = []

    class _LLM:
        def invoke(self, _prompt, **kwargs):
            seen.append(kwargs)
            if kwargs:
                raise ValueError("Extra inputs are not permitted")
            return "ok"

    monkeypatch.setattr(
        "core.model_catalog.get_model_spec",
        lambda mid: type("S", (), {"build_llm": lambda self: _LLM()})(),
    )
    ok, detail = probe("google_genai:m")
    assert ok and detail == ""
    assert seen == [{"max_tokens": 1}, {}]


def test_probe_reports_provider_rejection(monkeypatch) -> None:
    class _LLM:
        def invoke(self, _prompt, **kwargs):
            raise RuntimeError("404 NOT_FOUND: no longer available to new users")

    monkeypatch.setattr(
        "core.model_catalog.get_model_spec",
        lambda mid: type("S", (), {"build_llm": lambda self: _LLM()})(),
    )
    ok, detail = probe("google_genai:m")
    assert not ok
    assert "no longer available" in detail
