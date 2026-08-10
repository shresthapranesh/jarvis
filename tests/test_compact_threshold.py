"""Per-model sizing of the destructive-summarization trigger.

`compact_threshold` used to return one flat number for every model, so a
1M-token Gemini summarized at the same point as a model an order of magnitude
smaller. These guard the derivation and — more importantly — its failure modes,
since the read path for models is deliberately permissive (a conversation can be
pinned to a model id that no longer exists) and this runs on every agent build.
"""

from __future__ import annotations

import pytest

from core.compaction import (
    COMPACT_THRESHOLD_DEFAULT,
    COMPACT_THRESHOLD_MAX,
    COMPACT_THRESHOLD_MIN,
    compact_threshold,
)
from core.model_catalog import ModelSpec, load_custom_models, set_custom_models

_ENV_KEYS = ("JARVIS_COMPACT_TOKEN_THRESHOLD", "JARVIS_SUMMARIZE_TOKEN_THRESHOLD")


@pytest.fixture(autouse=True)
def _clean_env_and_catalog(monkeypatch):
    """No ambient override, and no custom models leaking between tests."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    set_custom_models(())


# ── Derivation ───────────────────────────────────────────────────────────────

def test_unknown_window_falls_back_to_flat_default():
    # Ollama entries carry no window on purpose: num_ctx governs the real limit
    # and this process can't see it.
    assert compact_threshold("ollama:llama3.3") == COMPACT_THRESHOLD_DEFAULT


def test_no_model_falls_back_to_flat_default():
    assert compact_threshold() == COMPACT_THRESHOLD_DEFAULT


def test_large_window_is_capped_not_uncapped():
    # 40% of 1,048,576 is ~419k, which would bill on every agent-loop iteration.
    # The cap is the point of the ceiling, so assert the cap and not the fraction.
    assert compact_threshold("google_genai:gemini-2.5-pro") == COMPACT_THRESHOLD_MAX


def test_large_window_raises_the_threshold_above_the_flat_default():
    """The whole point: a big-context model must not compact at the old number."""
    assert compact_threshold("google_genai:gemini-2.5-pro") > COMPACT_THRESHOLD_DEFAULT


def test_mid_window_uses_the_fraction():
    # 200k window → 40% → 80k, below the cap and above the floor.
    assert compact_threshold("bedrock:us.anthropic.claude-sonnet-4-6") == 80_000


def test_small_window_clamps_to_floor():
    set_custom_models((ModelSpec("ollama:tiny", "Tiny", "ollama", 4_096),))
    # 40% of 4096 is ~1.6k — low enough that the agent would summarize after
    # almost every tool call and never hold enough context to work.
    assert compact_threshold("ollama:tiny") == COMPACT_THRESHOLD_MIN


# ── Failure modes ────────────────────────────────────────────────────────────

def test_unknown_model_id_does_not_raise():
    """Read paths stay permissive — a stale pinned model must still run."""
    assert compact_threshold("provider:removed-last-year") == COMPACT_THRESHOLD_DEFAULT


def test_env_override_beats_the_per_model_derivation():
    import os

    os.environ["JARVIS_COMPACT_TOKEN_THRESHOLD"] = "1234"
    try:
        assert compact_threshold("google_genai:gemini-2.5-pro") == 1234
    finally:
        del os.environ["JARVIS_COMPACT_TOKEN_THRESHOLD"]


# ── Custom-model plumbing ────────────────────────────────────────────────────

def test_custom_model_window_round_trips_from_config_rows():
    load_custom_models([{"id": "anthropic:custom-1", "label": "C", "context_window": 500_000}])
    assert compact_threshold("anthropic:custom-1") == COMPACT_THRESHOLD_MAX


@pytest.mark.parametrize("bad", ["not-a-number", None, 0, -1, ""])
def test_unusable_custom_window_degrades_to_default(bad):
    """A bad window must read as 'unknown', never as a literal ceiling."""
    load_custom_models([{"id": "anthropic:custom-2", "label": "C", "context_window": bad}])
    assert compact_threshold("anthropic:custom-2") == COMPACT_THRESHOLD_DEFAULT


def test_missing_context_window_key_is_not_an_error():
    load_custom_models([{"id": "anthropic:custom-3", "label": "C"}])
    assert compact_threshold("anthropic:custom-3") == COMPACT_THRESHOLD_DEFAULT
