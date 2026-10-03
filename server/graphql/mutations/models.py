"""Model catalog mutations — the UI counterpart of `main.py model add/remove/set-default`.

Custom models are persisted as a JSON list under the `models.custom` config key
(db/ops.py) and merged with the compiled-in `BUILTIN_MODELS` by
`core.model_catalog.available_models`. Built-ins can be set as the default but
never edited or removed.

Every mutation returns the whole `ModelCatalog` so a client can replace its
catalog state from the response without a follow-up query.
"""

from __future__ import annotations

import strawberry

from core.model_catalog import (
    DEFAULT_MODEL,
    endpoint_name_error,
    is_builtin_model,
    is_valid_model,
    known_providers,
    provider_from_id,
)
from db.ops import (
    add_custom_model,
    get_custom_models,
    get_default_model,
    get_endpoint_rows,
    hydrate_catalog,
    remove_custom_model,
    set_endpoint_rows,
    set_setting,
)

from ..types.model_catalog import ModelCatalog, load_model_catalog
from ..types.model_sync import DiscoveredModelInput

_DEFAULT_MODEL_KEY = "default.model"


async def _catalog_changed(session) -> ModelCatalog:
    """Re-read the catalog after a write, dropping the compiled agent graphs.

    A graph is cached under the model id it was built for, and a run naming a
    model the catalog doesn't have is built from the default instead — so once a
    model is removed, its id caches a default-model graph. Re-adding it later
    would keep serving that graph until a restart. Editing the catalog is rare
    and rebuilding is cheap, so drop the cache on every write rather than
    reasoning about which edits can strand one.
    """
    from core.agents import invalidate_agent_cache

    catalog = await load_model_catalog(session)
    invalidate_agent_cache()
    return catalog


def _validated(model_id: str, provider: str | None) -> tuple[str, str]:
    """Normalize + validate a custom model id and provider (mirrors `model add`).
    Callers hydrate the catalog first, so an endpoint another process added
    counts as a provider."""
    model_id = model_id.strip()
    prov = (provider or provider_from_id(model_id)).strip()
    if ":" not in model_id or not model_id.partition(":")[2]:
        raise ValueError(
            f"Invalid model ID '{model_id}' — expected 'provider:model_name', "
            "e.g. google_genai:gemini-3.5-flash"
        )
    if prov not in known_providers():
        raise ValueError(
            f"Unsupported provider '{prov}' — must be one of: "
            f"{', '.join(sorted(known_providers()))}"
        )
    return model_id, prov


def _base_url(base_url: str) -> str:
    url = base_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Base URL must start with http:// or https:// — got '{base_url}'")
    return url


async def _endpoint_rows(session) -> list[dict]:
    """The stored endpoints as dict rows, the unusable ones dropped — a write
    rewrites the list, and keeps only what the catalog would load."""
    from core.model_catalog import parse_endpoints

    return [
        {"name": e.name, "base_url": e.base_url, **({"api_key": e.api_key} if e.api_key else {})}
        for e in parse_endpoints(await get_endpoint_rows(session))
    ]


@strawberry.type
class ModelsMutation:
    @strawberry.mutation
    async def add_model(
        self,
        info: strawberry.Info,
        id: str,
        label: str,
        provider: str | None = None,
    ) -> ModelCatalog:
        """Add a custom model to the catalog. `provider` defaults to the id prefix."""
        session = info.context["session"]
        await hydrate_catalog(session)
        model_id, prov = _validated(id, provider)
        if is_builtin_model(model_id):
            raise ValueError(f"'{model_id}' is a built-in model — it already exists")
        if any(m.get("id") == model_id for m in await get_custom_models(session)):
            raise ValueError(f"Model '{model_id}' already exists — edit it instead")
        await add_custom_model(session, model_id, label.strip() or model_id, prov)
        return await _catalog_changed(session)

    @strawberry.mutation
    async def update_model(
        self,
        info: strawberry.Info,
        id: str,
        label: str,
        provider: str | None = None,
    ) -> ModelCatalog:
        """Update a custom model's label/provider. The id is the key, so it can't
        change — remove and re-add to rename (conversations pin the id)."""
        session = info.context["session"]
        await hydrate_catalog(session)
        model_id, prov = _validated(id, provider)
        if is_builtin_model(model_id):
            raise ValueError(f"'{model_id}' is a built-in model and cannot be edited")
        if not any(m.get("id") == model_id for m in await get_custom_models(session)):
            raise ValueError(f"No custom model '{model_id}'")
        # add_custom_model upserts by id.
        await add_custom_model(session, model_id, label.strip() or model_id, prov)
        return await _catalog_changed(session)

    @strawberry.mutation
    async def add_discovered_models(
        self,
        info: strawberry.Info,
        models: list[DiscoveredModelInput],
    ) -> ModelCatalog:
        """Register models found by `modelSync` into the custom layer.

        The UI counterpart of `model sync --add-new`, except the operator picks
        which ones. Every entry is validated *before* anything is written, so a
        bad id in a batch of thirty can't leave half of them registered.

        Also the apply path for a context_window finding: `add_custom_model`
        upserts by id, so re-sending an existing custom model with the
        provider's window is how that gets corrected. A built-in's window is
        compiled in and is rejected here — it needs a code change.
        """
        session = info.context["session"]
        if not models:
            raise ValueError("No models given")
        await hydrate_catalog(session)

        validated: list[tuple[str, str, str, int | None]] = []
        for m in models:
            model_id, prov = _validated(m.id, m.provider)
            if is_builtin_model(model_id):
                raise ValueError(
                    f"'{model_id}' is a built-in model — its catalog entry, "
                    "including context_window, is compiled in and needs a code change"
                )
            window = m.context_window
            if window is not None and window <= 0:
                raise ValueError(f"'{model_id}': context_window must be positive")
            validated.append((model_id, m.label.strip() or model_id, prov, window))

        for model_id, label, prov, window in validated:
            await add_custom_model(session, model_id, label, prov, window)
        return await _catalog_changed(session)

    @strawberry.mutation
    async def remove_model(self, info: strawberry.Info, id: str) -> ModelCatalog:
        """Remove a custom model. Built-ins can't be removed."""
        session = info.context["session"]
        if is_builtin_model(id):
            raise ValueError(f"'{id}' is a built-in model and cannot be removed")
        if not await remove_custom_model(session, id):
            raise ValueError(f"No custom model '{id}'")
        # Don't leave the default pointing at a model that no longer exists —
        # read paths would silently fall back to DEFAULT_MODEL while the UI kept
        # showing the dead id.
        if await get_default_model(session) == id:
            await set_setting(session, _DEFAULT_MODEL_KEY, DEFAULT_MODEL)
        return await _catalog_changed(session)

    @strawberry.mutation
    async def set_default_model(self, info: strawberry.Info, id: str) -> ModelCatalog:
        """Persist the default model used when a request names none."""
        session = info.context["session"]
        # Hydrate the custom-model cache first so is_valid_model accepts
        # runtime-added models.
        await load_model_catalog(session)
        if not is_valid_model(id):
            raise ValueError(f"Unknown model '{id}'")
        await set_setting(session, _DEFAULT_MODEL_KEY, id)
        return await _catalog_changed(session)

    # ── OpenAI-compatible endpoints ──────────────────────────────────────────

    @strawberry.mutation
    async def add_endpoint(
        self,
        info: strawberry.Info,
        name: str,
        base_url: str,
        api_key: str | None = None,
    ) -> ModelCatalog:
        """Name an OpenAI-compatible server. Its models are then added as
        `name:model` — by hand, or from Sync, which lists its `/models`."""
        session = info.context["session"]
        name = name.strip()
        if error := endpoint_name_error(name):
            raise ValueError(error)
        rows = await _endpoint_rows(session)
        if any(r["name"] == name for r in rows):
            raise ValueError(f"Endpoint '{name}' already exists — edit it instead")
        row = {"name": name, "base_url": _base_url(base_url)}
        if api_key and api_key.strip():
            row["api_key"] = api_key.strip()
        await set_endpoint_rows(session, [*rows, row])
        return await _catalog_changed(session)

    @strawberry.mutation
    async def update_endpoint(
        self,
        info: strawberry.Info,
        name: str,
        base_url: str,
        api_key: str | None = None,
        clear_key: bool = False,
    ) -> ModelCatalog:
        """Change an endpoint's URL or key. The name can't change: its models'
        ids carry it. An absent `api_key` keeps the stored one (it is never
        sent to the browser, so the form can't send it back); `clear_key`
        drops it."""
        session = info.context["session"]
        rows = await _endpoint_rows(session)
        row = next((r for r in rows if r["name"] == name), None)
        if row is None:
            raise ValueError(f"No endpoint '{name}'")
        row["base_url"] = _base_url(base_url)
        if clear_key:
            row.pop("api_key", None)
        elif api_key and api_key.strip():
            row["api_key"] = api_key.strip()
        await set_endpoint_rows(session, rows)
        return await _catalog_changed(session)

    @strawberry.mutation
    async def remove_endpoint(self, info: strawberry.Info, name: str) -> ModelCatalog:
        """Remove an endpoint no custom model uses."""
        session = info.context["session"]
        rows = await _endpoint_rows(session)
        if not any(r["name"] == name for r in rows):
            raise ValueError(f"No endpoint '{name}'")
        using = [
            m.get("id") for m in await get_custom_models(session)
            if (m.get("provider") or provider_from_id(str(m.get("id") or ""))) == name
        ]
        if using:
            raise ValueError(f"Endpoint '{name}' is used by {', '.join(map(str, using))} — remove those first")
        await set_endpoint_rows(session, [r for r in rows if r["name"] != name])
        return await _catalog_changed(session)
