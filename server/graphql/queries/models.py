"""Model catalog query."""

from __future__ import annotations

import strawberry

from core.model_catalog import AVAILABLE_MODELS, DEFAULT_MODEL

from ..types.model_catalog import ModelCatalog, ModelSpec


@strawberry.type
class ModelsQuery:
    @strawberry.field
    def models(self) -> ModelCatalog:
        return ModelCatalog(
            default=DEFAULT_MODEL,
            available=[
                ModelSpec(id=m.id, label=m.label, provider=m.provider)
                for m in AVAILABLE_MODELS
            ],
        )
