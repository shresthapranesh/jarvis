"""Model catalog query."""

from __future__ import annotations

import strawberry

from ..types.model_catalog import ModelCatalog, load_model_catalog
from ..types.model_sync import ModelSyncReport, run_model_sync


@strawberry.type
class ModelsQuery:
    @strawberry.field
    async def models(self, info: strawberry.Info) -> ModelCatalog:
        # load_model_catalog re-hydrates the in-memory custom-model cache from
        # the DB, so models added at runtime (e.g. via `main.py model add`) show
        # up without a server restart.
        return await load_model_catalog(info.context["session"])

    @strawberry.field
    async def model_sync(
        self,
        info: strawberry.Info,
        provider: str | None = None,
        probe: bool = False,
    ) -> list[ModelSyncReport]:
        """Diff the catalog against what each provider actually offers.

        Read-only — it reports drift; `addDiscoveredModels` is what acts on it.
        `probe` additionally issues a real one-token call per catalog model,
        which is the only thing that distinguishes "published" from "callable by
        this credential". It costs a request per model, so it is opt-in here for
        the same reason it is a flag on the CLI.
        """
        return await run_model_sync(info.context["session"], provider, probe)
