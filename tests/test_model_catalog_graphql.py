"""Model-catalog CRUD over GraphQL — the UI counterpart of `main.py model *`.

Custom models live in one JSON blob under the `models.custom` config key and are
merged with the compiled-in BUILTIN_MODELS by core.model_catalog. That merge runs
off a process-global cache, so these tests also pin the two things the cache can
get wrong: a mutation's own return value must already reflect its write, and a
built-in must never be editable or removable.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

CATALOG_FIELDS = "{ default providers available { id label provider builtin } }"

CUSTOM_ID = "google_genai:gemini-test-9000"


def _context(session):
    from server.graphql.extensions import SESSION_LOCK_KEY

    return {"session": session, SESSION_LOCK_KEY: asyncio.Lock()}


async def _exec(query: str, variables: dict[str, Any] | None = None) -> Any:
    from db import async_session
    from server.graphql.schema import schema

    async with async_session() as s:
        return await schema.execute(query, variable_values=variables, context_value=_context(s))


async def _catalog() -> dict[str, Any]:
    res = await _exec("{ models %s }" % CATALOG_FIELDS)
    assert not res.errors, res.errors
    return res.data["models"]


def _by_id(catalog: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    return next((m for m in catalog["available"] if m["id"] == model_id), None)


@pytest.fixture
def catalog_cache():
    """Restore the process-global custom-model cache after each test."""
    from core.model_catalog import _custom_models, set_custom_models

    yield
    set_custom_models(_custom_models)


async def test_add_model_appears_in_catalog(database, catalog_cache):
    res = await _exec(
        'mutation { addModel(id: "%s", label: "Gemini Test") %s }' % (CUSTOM_ID, CATALOG_FIELDS)
    )
    assert not res.errors, res.errors

    # The mutation's own return value must already include the new model —
    # load_model_catalog re-hydrates the cache after the write.
    added = _by_id(res.data["addModel"], CUSTOM_ID)
    assert added == {
        "id": CUSTOM_ID,
        "label": "Gemini Test",
        "provider": "google_genai",  # inferred from the id prefix
        "builtin": False,
    }
    # ...and it survives into a fresh request.
    assert _by_id(await _catalog(), CUSTOM_ID) == added


async def test_add_rejects_bad_id_and_unknown_provider(database, catalog_cache):
    for bad_id in ("no-colon", "google_genai:"):
        res = await _exec('mutation { addModel(id: "%s", label: "x") { default } }' % bad_id)
        assert res.errors, f"{bad_id!r} should not be accepted"
        assert "provider:model_name" in str(res.errors[0])

    res = await _exec('mutation { addModel(id: "openai:gpt-9", label: "x") { default } }')
    assert res.errors
    assert "Unsupported provider" in str(res.errors[0])

    # Nothing was persisted by the rejected writes.
    catalog = await _catalog()
    assert all(m["builtin"] for m in catalog["available"])


async def test_add_rejects_duplicates(database, catalog_cache):
    from core.model_catalog import DEFAULT_MODEL

    await _exec('mutation { addModel(id: "%s", label: "a") { default } }' % CUSTOM_ID)

    res = await _exec('mutation { addModel(id: "%s", label: "b") { default } }' % CUSTOM_ID)
    assert res.errors
    assert "already exists" in str(res.errors[0])

    res = await _exec('mutation { addModel(id: "%s", label: "b") { default } }' % DEFAULT_MODEL)
    assert res.errors
    assert "built-in" in str(res.errors[0])


async def test_update_model_changes_label_and_provider(database, catalog_cache):
    await _exec('mutation { addModel(id: "%s", label: "old") { default } }' % CUSTOM_ID)

    res = await _exec(
        'mutation { updateModel(id: "%s", label: "new", provider: "ollama") %s }'
        % (CUSTOM_ID, CATALOG_FIELDS)
    )
    assert not res.errors, res.errors
    updated = _by_id(res.data["updateModel"], CUSTOM_ID)
    assert updated is not None
    assert updated["label"] == "new"
    assert updated["provider"] == "ollama"

    # An update must not duplicate the row (add_custom_model upserts by id).
    catalog = await _catalog()
    assert [m["id"] for m in catalog["available"]].count(CUSTOM_ID) == 1


async def test_update_rejects_builtin_and_unknown(database, catalog_cache):
    from core.model_catalog import DEFAULT_MODEL

    res = await _exec('mutation { updateModel(id: "%s", label: "x") { default } }' % DEFAULT_MODEL)
    assert res.errors
    assert "built-in" in str(res.errors[0])

    res = await _exec('mutation { updateModel(id: "%s", label: "x") { default } }' % CUSTOM_ID)
    assert res.errors
    assert "No custom model" in str(res.errors[0])


async def test_remove_model(database, catalog_cache):
    from core.model_catalog import DEFAULT_MODEL

    await _exec('mutation { addModel(id: "%s", label: "x") { default } }' % CUSTOM_ID)

    res = await _exec('mutation { removeModel(id: "%s") %s }' % (CUSTOM_ID, CATALOG_FIELDS))
    assert not res.errors, res.errors
    assert _by_id(res.data["removeModel"], CUSTOM_ID) is None
    assert _by_id(await _catalog(), CUSTOM_ID) is None

    # Built-ins are compiled in — there is nothing to remove.
    res = await _exec('mutation { removeModel(id: "%s") { default } }' % DEFAULT_MODEL)
    assert res.errors
    assert "built-in" in str(res.errors[0])


async def test_set_default_model(database, catalog_cache):
    from core.model_catalog import BUILTIN_MODELS

    target = BUILTIN_MODELS[1].id
    res = await _exec('mutation { setDefaultModel(id: "%s") %s }' % (target, CATALOG_FIELDS))
    assert not res.errors, res.errors
    assert res.data["setDefaultModel"]["default"] == target
    assert (await _catalog())["default"] == target

    # A runtime-added model is a valid default — validation runs after the
    # custom-model cache is re-hydrated, not against BUILTIN_MODELS alone.
    await _exec('mutation { addModel(id: "%s", label: "x") { default } }' % CUSTOM_ID)
    res = await _exec('mutation { setDefaultModel(id: "%s") { default } }' % CUSTOM_ID)
    assert not res.errors, res.errors
    assert (await _catalog())["default"] == CUSTOM_ID

    res = await _exec('mutation { setDefaultModel(id: "ollama:nope-not-real") { default } }')
    assert res.errors
    assert "Unknown model" in str(res.errors[0])
    assert (await _catalog())["default"] == CUSTOM_ID  # unchanged


async def test_removing_the_default_resets_it(database, catalog_cache):
    """Otherwise `default.model` points at a model that no longer exists: read
    paths silently fall back to DEFAULT_MODEL while the UI shows the dead id."""
    from core.model_catalog import DEFAULT_MODEL

    await _exec('mutation { addModel(id: "%s", label: "x") { default } }' % CUSTOM_ID)
    await _exec('mutation { setDefaultModel(id: "%s") { default } }' % CUSTOM_ID)

    res = await _exec('mutation { removeModel(id: "%s") %s }' % (CUSTOM_ID, CATALOG_FIELDS))
    assert not res.errors, res.errors
    assert res.data["removeModel"]["default"] == DEFAULT_MODEL
    assert (await _catalog())["default"] == DEFAULT_MODEL


async def test_catalog_reports_providers_and_builtin_flags(database, catalog_cache):
    from core.model_catalog import BUILTIN_MODELS, KNOWN_PROVIDERS

    catalog = await _catalog()
    assert catalog["providers"] == sorted(KNOWN_PROVIDERS)
    builtin_ids = {m.id for m in BUILTIN_MODELS}
    for m in catalog["available"]:
        assert m["builtin"] == (m["id"] in builtin_ids)
