"""Model catalog query."""

from __future__ import annotations

import strawberry

from core.model_catalog import available_models, load_custom_models
from db.ops import get_custom_models, get_default_model

from ..types.model_catalog import ModelCatalog, ModelSpec


@strawberry.type
class ModelsQuery:
    @strawberry.field
    async def models(self, info: strawberry.Info) -> ModelCatalog:
        # Re-hydrate the in-memory custom-model cache from the DB so models
        # added at runtime (e.g. via `main.py model add`) show up without a
        # server restart, and surface the persisted default.
        session = info.context["session"]
        load_custom_models(await get_custom_models(session))
        return ModelCatalog(
            default=await get_default_model(session),
            available=[
                ModelSpec(id=m.id, label=m.label, provider=m.provider)
                for m in available_models()
            ],
        )
