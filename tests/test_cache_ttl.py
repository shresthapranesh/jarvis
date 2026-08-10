"""Cache-block TTL resolution and the shape of the emitted cache_control.

The default has to stay byte-identical to what we sent before this was
configurable — an added request field is an added way to break every call — so
these pin the emitted payload, not just the resolved string.
"""

from __future__ import annotations

import pytest

from core.context_cache import (
    ContextCacheConfig,
    build_cached_system_message,
    resolve_cache_ttl,
)

_ENV = "JARVIS_CACHE_TTL"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)


# ── Resolution ───────────────────────────────────────────────────────────────

def test_defaults_to_5m_when_unset():
    assert resolve_cache_ttl("anthropic") == "5m"


def test_1h_honoured_for_anthropic(monkeypatch):
    monkeypatch.setenv(_ENV, "1h")
    assert resolve_cache_ttl("anthropic") == "1h"


@pytest.mark.parametrize("provider", ["bedrock", "google_genai", "ollama", "meta"])
def test_1h_ignored_for_providers_without_extended_ttl(monkeypatch, provider):
    """Bedrock especially: an unsupported field there fails every call, so the
    extended TTL stays anthropic-only until someone verifies the Converse API."""
    monkeypatch.setenv(_ENV, "1h")
    assert resolve_cache_ttl(provider) == "5m"


@pytest.mark.parametrize("bad", ["", "  ", "30m", "1hour", "forever", "0"])
def test_invalid_ttl_falls_back_to_default(monkeypatch, bad):
    monkeypatch.setenv(_ENV, bad)
    assert resolve_cache_ttl("anthropic") == "5m"


def test_whitespace_is_tolerated(monkeypatch):
    monkeypatch.setenv(_ENV, "  1h  ")
    assert resolve_cache_ttl("anthropic") == "1h"


# ── Emitted payload ──────────────────────────────────────────────────────────

def _cache_controls(cfg: ContextCacheConfig) -> list[dict]:
    from core.context_cache import CacheSegment

    msg, _ = build_cached_system_message(
        static_prompt="S" * 400,
        segments=[CacheSegment(name="core_memory", content="M" * 400)],
        volatile_suffix="volatile bits",
        use_cache=True,
        config=cfg,
    )
    assert isinstance(msg.content, list)
    return [b["cache_control"] for b in msg.content if isinstance(b, dict) and "cache_control" in b]


def test_default_emits_no_ttl_key():
    """The whole point: at 5m the body must be what it always was."""
    controls = _cache_controls(ContextCacheConfig(enabled=True))
    assert controls, "expected at least one cached block"
    assert all(c == {"type": "ephemeral"} for c in controls)


def test_1h_emits_ttl_on_every_cached_block():
    controls = _cache_controls(ContextCacheConfig(enabled=True, cache_ttl="1h"))
    assert controls
    assert all(c == {"type": "ephemeral", "ttl": "1h"} for c in controls)


def test_volatile_block_is_never_given_cache_control():
    from core.context_cache import CacheSegment

    msg, _ = build_cached_system_message(
        static_prompt="S" * 400,
        segments=[CacheSegment(name="relevant_memories", content="R" * 400, cacheable=False)],
        volatile_suffix="changes every turn",
        use_cache=True,
        config=ContextCacheConfig(enabled=True, cache_ttl="1h"),
    )
    blocks = [b for b in msg.content if isinstance(b, dict)]
    tail = blocks[-1]
    assert "cache_control" not in tail
    assert "changes every turn" in tail["text"]


def test_uncached_path_emits_plain_text_regardless_of_ttl():
    from core.context_cache import CacheSegment

    msg, _ = build_cached_system_message(
        static_prompt="S" * 400,
        segments=[CacheSegment(name="core_memory", content="M" * 400)],
        volatile_suffix="",
        use_cache=False,
        config=ContextCacheConfig(enabled=False, cache_ttl="1h"),
    )
    assert isinstance(msg.content, str)
