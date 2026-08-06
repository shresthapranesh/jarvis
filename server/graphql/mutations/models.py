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
    KNOWN_PROVIDERS,
    is_builtin_model,
    is_valid_model,
    provider_from_id,
)
from db.ops import (
    add_custom_model,
    get_custom_models,
    get_default_model,
    remove_custom_model,
    set_setting,
)

from ..types.model_catalog import ModelCatalog, load_model_catalog

_DEFAULT_MODEL_KEY = "default.model"


def _validated(model_id: str, provider: str | None) -> tuple[str, str]:
    """Normalize + validate a custom model id and provider (mirrors `model add`)."""
    model_id = model_id.strip()
    prov = (provider or provider_from_id(model_id)).strip()
    if ":" not in model_id or not model_id.partition(":")[2]:
        raise ValueError(
            f"Invalid model ID '{model_id}' — expected 'provider:model_name', "
            "e.g. google_genai:gemini-3.5-flash"
        )
    if prov not in KNOWN_PROVIDERS:
        raise ValueError(
            f"Unsupported provider '{prov}' — must be one of: "
            f"{', '.join(sorted(KNOWN_PROVIDERS))}"
        )
    return model_id, prov


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
        model_id, prov = _validated(id, provider)
        if is_builtin_model(model_id):
            raise ValueError(f"'{model_id}' is a built-in model — it already exists")
        if any(m.get("id") == model_id for m in await get_custom_models(session)):
            raise ValueError(f"Model '{model_id}' already exists — edit it instead")
        await add_custom_model(session, model_id, label.strip() or model_id, prov)
        return await load_model_catalog(session)

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
        model_id, prov = _validated(id, provider)
        if is_builtin_model(model_id):
            raise ValueError(f"'{model_id}' is a built-in model and cannot be edited")
        if not any(m.get("id") == model_id for m in await get_custom_models(session)):
            raise ValueError(f"No custom model '{model_id}'")
        # add_custom_model upserts by id.
        await add_custom_model(session, model_id, label.strip() or model_id, prov)
        return await load_model_catalog(session)

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
        return await load_model_catalog(session)

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
        return await load_model_catalog(session)
