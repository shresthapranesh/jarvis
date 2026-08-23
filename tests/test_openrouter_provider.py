"""OpenRouter as a catalog provider.

OpenRouter is an OpenAI-wire-compatible router in front of many upstreams, so
two things that are provider-level facts everywhere else become model-level
facts here: which upstream serves the call, and whether prompt caching applies.
These pin both, plus the client construction, without touching the network.
"""

from __future__ import annotations

import pytest

from core.model_catalog import (
    KNOWN_PROVIDERS,
    OPENROUTER_BASE_URL,
    ModelSpec,
    honors_cache_control,
)
from core.model_discovery import DISCOVERABLE, discover

_CACHE_PROVIDERS = frozenset({"bedrock", "anthropic", "openrouter"})


def _spec(mid: str) -> ModelSpec:
    return ModelSpec(mid, mid, mid.partition(":")[0])


def test_openrouter_is_a_known_and_discoverable_provider() -> None:
    assert "openrouter" in KNOWN_PROVIDERS
    assert "openrouter" in DISCOVERABLE


def test_build_llm_requires_the_key_and_names_it(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        _spec("openrouter:anthropic/claude-sonnet-4.5").build_llm()


def test_build_llm_points_the_openai_client_at_openrouter(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    llm = _spec("openrouter:anthropic/claude-sonnet-4.5").build_llm()
    assert llm.openai_api_base == OPENROUTER_BASE_URL
    assert llm.model_name == "anthropic/claude-sonnet-4.5"
    # Not cosmetic: ChatOpenAI defaults stream_usage to None (→ False) and every
    # run in this app streams, so without it usage_metadata never arrives and
    # BudgetTracker/PerfTracker measure zero tokens forever.
    assert llm.stream_usage is True


def test_variant_suffix_survives_into_the_model_name(monkeypatch) -> None:
    """`deepseek/deepseek-r1:free` has its own colon — the id splits on the
    first one only, so the variant must not be truncated off."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    llm = _spec("openrouter:deepseek/deepseek-r1:free").build_llm()
    assert llm.model_name == "deepseek/deepseek-r1:free"


@pytest.mark.parametrize(
    ("model_id", "cached"),
    [
        ("openrouter:anthropic/claude-sonnet-4.5", True),
        ("openrouter:anthropic/claude-3.7-sonnet", True),
        # `~name` is OpenRouter's alias route to the same upstream.
        ("openrouter:~anthropic/claude-sonnet-latest", True),
        ("openrouter:openai/gpt-5", False),        # caches automatically, not via our blocks
        ("openrouter:deepseek/deepseek-r1", False),
        ("anthropic:claude-opus-4-7", True),
        ("google_genai:gemini-2.5-pro", False),
    ],
)
def test_cache_control_is_decided_per_upstream(model_id: str, cached: bool) -> None:
    assert honors_cache_control(_spec(model_id), _CACHE_PROVIDERS) is cached


def test_cache_gating_still_respects_the_provider_switch() -> None:
    """Dropping openrouter from cache_enabled_providers turns it off wholesale,
    including for anthropic upstreams."""
    spec = _spec("openrouter:anthropic/claude-sonnet-4.5")
    assert honors_cache_control(spec, {"anthropic", "bedrock"}) is False


# ── Discovery ────────────────────────────────────────────────────────────────

_LISTING = {
    "data": [
        {
            "id": "anthropic/claude-sonnet-4.5",
            "name": "Anthropic: Claude Sonnet 4.5",
            "context_length": 200000,
            "description": "  hybrid reasoning  ",
            "architecture": {"output_modalities": ["text"]},
        },
        {
            "id": "google/veo-3",
            "name": "Google: Veo 3",
            "architecture": {"output_modalities": ["video"]},
        },
        {
            # No architecture block and no top-level window — the parser must
            # fall back rather than drop the model or invent a number.
            "id": "deepseek/deepseek-r1:free",
            "name": "DeepSeek R1 (free)",
            "top_provider": {"context_length": 64000},
        },
        {"name": "no id at all"},
    ]
}


class _Resp:
    status_code = 200

    def raise_for_status(self) -> None:  # pragma: no cover - trivial
        pass

    def json(self) -> dict:
        return _LISTING


def test_discovery_parses_the_listing(monkeypatch) -> None:
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
    found = {m.id: m for m in discover("openrouter")}

    assert set(found) == {
        "openrouter:anthropic/claude-sonnet-4.5",
        "openrouter:google/veo-3",
        "openrouter:deepseek/deepseek-r1:free",
    }

    sonnet = found["openrouter:anthropic/claude-sonnet-4.5"]
    assert sonnet.context_window == 200_000
    assert sonnet.description == "hybrid reasoning"
    assert sonnet.likely_chat

    # OpenRouter publishes output modalities, so this is a stated fact rather
    # than the name heuristic Google's listing forces.
    assert found["openrouter:google/veo-3"].likely_chat is False
    assert found["openrouter:google/veo-3"].context_window is None

    # Falls back to top_provider.context_length, and keeps the `:free` variant.
    assert found["openrouter:deepseek/deepseek-r1:free"].context_window == 64_000


def test_discovery_reports_an_unreachable_router_as_a_skip(monkeypatch) -> None:
    from core.model_discovery import DiscoveryError

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("httpx.get", boom)
    with pytest.raises(DiscoveryError, match="could not reach OpenRouter"):
        discover("openrouter")
