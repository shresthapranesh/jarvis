"""`modelSync` + `addDiscoveredModels` — the UI's half of `main.py model sync`.

Discovery is stubbed throughout: what matters here is that reading a provider's
listing and *acting* on it stay two separate steps, and that a report can never
quietly rewrite the catalog.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.model_discovery import DiscoveredModel, DiscoveryError

SYNC_FIELDS = """{
  provider offered skipped probed clean missing
  unreachable { id reason }
  windows { id label provider catalogWindow providerWindow builtin }
  newModels { id label provider contextWindow description likelyChat }
}"""

CATALOG_FIELDS = "{ default providers discoverableProviders available { id label provider builtin contextWindow } }"


def _context(session):
    from server.graphql.extensions import SESSION_LOCK_KEY

    return {"session": session, SESSION_LOCK_KEY: asyncio.Lock()}


async def _exec(query: str, variables: dict[str, Any] | None = None) -> Any:
    from db import async_session
    from server.graphql.schema import schema

    async with async_session() as s:
        return await schema.execute(query, variable_values=variables, context_value=_context(s))


@pytest.fixture
def catalog_cache():
    from core.model_catalog import _custom_models, set_custom_models

    yield
    set_custom_models(_custom_models)


@pytest.fixture
def only_openrouter(monkeypatch):
    """Narrow discovery to one provider so a sync doesn't reach the network."""
    monkeypatch.setattr("core.model_discovery.DISCOVERABLE", frozenset({"openrouter"}))


def _stub_discovery(monkeypatch, models: list[DiscoveredModel] | Exception) -> None:
    def fake(provider: str):
        if isinstance(models, Exception):
            raise models
        return models

    monkeypatch.setattr("core.model_discovery.discover", fake)


async def test_sync_reports_new_models_without_touching_the_catalog(
    database, catalog_cache, only_openrouter, monkeypatch,
) -> None:
    _stub_discovery(monkeypatch, [
        DiscoveredModel(
            id="openrouter:anthropic/claude-sonnet-4.5", label="Claude Sonnet 4.5",
            provider="openrouter", context_window=200_000, description="d", likely_chat=True,
        ),
        DiscoveredModel(
            id="openrouter:google/veo-3", label="Veo 3", provider="openrouter",
            likely_chat=False,
        ),
    ])

    res = await _exec("{ modelSync %s }" % SYNC_FIELDS)
    assert not res.errors, res.errors
    (report,) = res.data["modelSync"]
    assert report["provider"] == "openrouter"
    assert report["offered"] == 2
    assert report["skipped"] is None
    assert report["probed"] is False
    assert report["clean"] is False
    assert {m["id"] for m in report["newModels"]} == {
        "openrouter:anthropic/claude-sonnet-4.5", "openrouter:google/veo-3",
    }
    # Advisory, not a filter: the non-chat model is still reported.
    assert [m["likelyChat"] for m in report["newModels"] if m["id"].endswith("veo-3")] == [False]

    # Read-only — nothing was registered.
    catalog = (await _exec("{ models %s }" % CATALOG_FIELDS)).data["models"]
    assert all(m["builtin"] for m in catalog["available"])


async def test_a_provider_that_cannot_be_reached_is_skipped_not_clean(
    database, catalog_cache, only_openrouter, monkeypatch,
) -> None:
    """A skipped provider has empty finding lists for lack of *data*. Rendering
    that as "in sync" would be the exact lie this report exists to prevent."""
    _stub_discovery(monkeypatch, DiscoveryError("could not reach OpenRouter: refused"))

    res = await _exec("{ modelSync %s }" % SYNC_FIELDS)
    assert not res.errors, res.errors
    (report,) = res.data["modelSync"]
    assert "could not reach" in report["skipped"]
    assert report["offered"] == 0
    assert report["clean"] is False


async def test_sync_rejects_a_provider_with_no_adapter(database, catalog_cache) -> None:
    res = await _exec('{ modelSync(provider: "meta") { provider } }')
    assert res.errors
    assert "No discovery adapter" in str(res.errors[0])

    res = await _exec('{ modelSync(provider: "nope") { provider } }')
    assert res.errors
    assert "Unknown provider" in str(res.errors[0])


async def test_window_finding_flags_builtins_as_unapplicable(
    database, catalog_cache, only_openrouter, monkeypatch,
) -> None:
    from core.model_catalog import BUILTIN_MODELS

    # A built-in whose catalog window is None, re-labelled as an openrouter
    # model so the diff picks it up under the stubbed provider.
    builtin = next(m for m in BUILTIN_MODELS if m.context_window is None)
    monkeypatch.setattr(
        "core.model_catalog.available_models",
        lambda: (builtin.__class__(builtin.id, builtin.label, "openrouter"),),
    )
    _stub_discovery(monkeypatch, [
        DiscoveredModel(id=builtin.id, label=builtin.label, provider="openrouter",
                        context_window=131_072),
    ])

    res = await _exec("{ modelSync %s }" % SYNC_FIELDS)
    assert not res.errors, res.errors
    (report,) = res.data["modelSync"]
    (finding,) = report["windows"]
    assert finding["catalogWindow"] is None
    assert finding["providerWindow"] == 131_072
    assert finding["builtin"] is True


async def test_add_discovered_models_registers_the_window(database, catalog_cache) -> None:
    res = await _exec(
        'mutation { addDiscoveredModels(models: ['
        '{id: "openrouter:anthropic/claude-sonnet-4.5", label: "Claude Sonnet 4.5", '
        'contextWindow: 200000}]) %s }' % CATALOG_FIELDS
    )
    assert not res.errors, res.errors
    added = next(
        m for m in res.data["addDiscoveredModels"]["available"]
        if m["id"] == "openrouter:anthropic/claude-sonnet-4.5"
    )
    assert added["provider"] == "openrouter"   # inferred from the id prefix
    assert added["contextWindow"] == 200_000

    # The window is what sizes compaction, so it has to survive the round trip
    # into the catalog rather than only appear in the mutation's response. A
    # 1M-token window is used deliberately: 40% of it clamps to 200k, which is
    # distinguishable from the flat 80k an unknown window falls back to.
    res = await _exec(
        'mutation { addDiscoveredModels(models: ['
        '{id: "openrouter:google/gemini-2.5-pro", label: "Gemini 2.5 Pro", '
        'contextWindow: 1048576}]) { default } }'
    )
    assert not res.errors, res.errors

    from core.compaction import compact_threshold

    assert compact_threshold("openrouter:google/gemini-2.5-pro") == 200_000
    assert compact_threshold("openrouter:nothing/registered") == 80_000


async def test_a_label_edit_does_not_drop_a_registered_window(database, catalog_cache) -> None:
    """add_custom_model upserts, and updateModel doesn't carry a window — so
    without preserve-on-None an edit would silently discard it."""
    await _exec(
        'mutation { addDiscoveredModels(models: ['
        '{id: "openrouter:x/y", label: "Y", contextWindow: 123456}]) { default } }'
    )
    res = await _exec(
        'mutation { updateModel(id: "openrouter:x/y", label: "Renamed") %s }' % CATALOG_FIELDS
    )
    assert not res.errors, res.errors
    row = next(m for m in res.data["updateModel"]["available"] if m["id"] == "openrouter:x/y")
    assert row["label"] == "Renamed"
    assert row["contextWindow"] == 123_456


async def test_add_discovered_validates_the_whole_batch_first(database, catalog_cache) -> None:
    from core.model_catalog import DEFAULT_MODEL

    res = await _exec(
        'mutation { addDiscoveredModels(models: ['
        '{id: "openrouter:good/one", label: "Good"}, '
        '{id: "%s", label: "Built-in"}]) { default } }' % DEFAULT_MODEL
    )
    assert res.errors
    assert "built-in" in str(res.errors[0])

    # The valid entry in the same batch must not have landed.
    catalog = (await _exec("{ models %s }" % CATALOG_FIELDS)).data["models"]
    assert all(m["id"] != "openrouter:good/one" for m in catalog["available"])


async def test_catalog_advertises_discoverable_providers(database, catalog_cache) -> None:
    from core.model_catalog import KNOWN_PROVIDERS
    from core.model_discovery import DISCOVERABLE

    catalog = (await _exec("{ models %s }" % CATALOG_FIELDS)).data["models"]
    assert catalog["discoverableProviders"] == sorted(KNOWN_PROVIDERS & DISCOVERABLE)
    assert set(catalog["discoverableProviders"]) <= set(catalog["providers"])
