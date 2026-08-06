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
