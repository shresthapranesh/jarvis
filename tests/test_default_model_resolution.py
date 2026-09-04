"""Null-model runs must honour `default.model`, not the compile-time seed.

`core.model_catalog.DEFAULT_MODEL` is the first entry of BUILTIN_MODELS — a
compiled-in seed, not the operator's choice. Every `x or DEFAULT_MODEL` fallback
therefore routed a run whose model was unset to a provider the user may never
have configured, and did it silently: the model only surfaced once that provider
failed, naming a model nobody selected. These tests pin the precedence so the
fallback can't regress to the constant.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("jarvis")

OPERATOR_DEFAULT = "anthropic:claude-sonnet-4-6"


async def _set_operator_default(model_id: str) -> None:
    from db import async_session
    from db.ops import set_setting

    async with async_session() as s:
        await set_setting(s, "default.model", model_id)


async def test_resolve_model_prefers_explicit_over_default():
    """An explicit model always wins — the default is only a fallback."""
    from db.ops import resolve_model

    await _set_operator_default(OPERATOR_DEFAULT)
    assert await resolve_model("google_genai:gemini-2.5-pro") == "google_genai:gemini-2.5-pro"


async def test_resolve_model_falls_back_to_configured_default():
    """Null/empty resolves to `default.model`, NOT the compile-time constant."""
    from core.model_catalog import DEFAULT_MODEL
    from db.ops import resolve_model

    await _set_operator_default(OPERATOR_DEFAULT)
    assert OPERATOR_DEFAULT != DEFAULT_MODEL, "test needs a default that differs from the seed"

    assert await resolve_model(None) == OPERATOR_DEFAULT
    assert await resolve_model("") == OPERATOR_DEFAULT


async def test_resolve_model_uses_catalog_seed_when_unconfigured():
    """With no `default.model` row at all, the compiled-in seed is correct."""
    from core.model_catalog import DEFAULT_MODEL
    from db.ops import resolve_model

    assert await resolve_model(None) == DEFAULT_MODEL


async def test_automation_without_model_runs_on_configured_default():
    """The regression: a null-model automation used to run on the seed model.

    Covers `_resolve_model`, which feeds the agent, the stateful conversation
    row, and the `task created: … model=` log line — a bare `model=-` there is
    what hid the wrong-provider run in the first place.
    """
    from db import async_session
    from db.models import Automation
    from server.automation_runtime import _resolve_model

    await _set_operator_default(OPERATOR_DEFAULT)

    unset = Automation(id="a-unset", name="unset", input_type="prompt", model=None)
    assert await _resolve_model(unset) == OPERATOR_DEFAULT

    explicit = Automation(
        id="a-explicit", name="explicit", input_type="prompt",
        model="google_genai:gemini-2.0-flash",
    )
    assert await _resolve_model(explicit) == "google_genai:gemini-2.0-flash"

    # Session-passing overload resolves identically.
    async with async_session() as s:
        assert await _resolve_model(unset, s) == OPERATOR_DEFAULT


# ── A stored id can outlive the model it names ────────────────────────────────
#
# The catalog is editable at runtime (Settings → Models, `main.py model remove`)
# while model ids live on long-lived rows — an automation, a conversation, a
# board task, a workflow node. Removing a custom model therefore leaves those
# rows pointing at nothing, and the run died with `Unknown model '…'` at
# `get_model_spec`. The model is *how* the work is carried out, not the work:
# these pin the degrade-and-log behaviour.

STALE = "openrouter:vendor/model-that-was-removed"


async def test_resolve_model_drops_a_model_the_catalog_no_longer_has():
    from db.ops import resolve_model

    await _set_operator_default(OPERATOR_DEFAULT)
    assert await resolve_model(STALE) == OPERATOR_DEFAULT


async def test_resolve_model_falls_past_a_stale_default_to_the_seed():
    """Both layers can be dead — `default.model` is writable from Settings → Config."""
    from core.model_catalog import DEFAULT_MODEL
    from db.ops import resolve_model

    await _set_operator_default(STALE)
    assert await resolve_model(None) == DEFAULT_MODEL
    assert await resolve_model("another:stale-one") == DEFAULT_MODEL


async def test_resolve_model_rehydrates_before_rejecting_an_id():
    """A model added by another process must not be demoted to the default.

    The custom-model cache is per-process, so an id registered by the CLI while
    the server is up is absent from it — indistinguishable from a removed model
    unless the config row is re-read on the miss.
    """
    from core.model_catalog import is_valid_model, set_custom_models
    from db import async_session
    from db.ops import add_custom_model, resolve_model

    await _set_operator_default(OPERATOR_DEFAULT)
    custom = "anthropic:claude-added-elsewhere"
    async with async_session() as s:
        await add_custom_model(s, custom, "Added elsewhere", "anthropic")
    set_custom_models(())  # this process never saw the write
    assert not is_valid_model(custom)

    assert await resolve_model(custom) == custom


async def test_build_agent_degrades_instead_of_raising_on_a_stale_id():
    """The last line of defence: a queued job carries its model id in its payload,
    so an id can go stale between enqueue and claim, after resolve_model ran."""
    from core.agents import build_agent
    from langgraph.checkpoint.memory import MemorySaver

    assert build_agent(STALE, checkpointer=MemorySaver()) is not None


async def test_automation_with_a_removed_model_runs_on_the_default():
    from db.models import Automation
    from server.automation_runtime import _resolve_model

    await _set_operator_default(OPERATOR_DEFAULT)
    auto = Automation(id="a-stale", name="stale", input_type="prompt", model=STALE)
    assert await _resolve_model(auto) == OPERATOR_DEFAULT
