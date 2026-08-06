"""Model catalog query."""

from __future__ import annotations

import strawberry

from ..types.model_catalog import ModelCatalog, load_model_catalog


@strawberry.type
class ModelsQuery:
    @strawberry.field
    async def models(self, info: strawberry.Info) -> ModelCatalog:
        # load_model_catalog re-hydrates the in-memory custom-model cache from
        # the DB, so models added at runtime (e.g. via `main.py model add`) show
        # up without a server restart.
        return await load_model_catalog(info.context["session"])
