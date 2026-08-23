"""GraphQL types for the model catalog."""

from __future__ import annotations

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession


@strawberry.type
class ModelSpec:
    id: str
    label: str
    provider: str
    # Built-ins are compiled into core.model_catalog.BUILTIN_MODELS: they can be
    # set as the default but never edited or removed.
    builtin: bool = False
    # Input token limit, or null when unknown — null is the documented safe
    # value (a flat compaction default), not a gap the UI should fill in.
    context_window: int | None = None


@strawberry.type
class ModelCatalog:
    default: str
    available: list[ModelSpec]
    # Providers `ModelSpec.build_llm` knows how to instantiate — a custom model's
    # id must be prefixed with one of these. Drives the provider picker in the UI.
    providers: list[str] = strawberry.field(default_factory=list)
    # The subset of `providers` that can enumerate their own models
    # (core.model_discovery.DISCOVERABLE). Drives the sync picker; a provider
    # missing from here has no adapter, which is not the same as offering
    # nothing.
    discoverable_providers: list[str] = strawberry.field(default_factory=list)


async def load_model_catalog(session: AsyncSession) -> ModelCatalog:
    """The full catalog, re-hydrating the custom-model cache from the DB first.

    Shared by the `models` query and every model mutation, so a mutation's
    return value already reflects the write it just made.
    """
    from core.model_catalog import (
        KNOWN_PROVIDERS,
        available_models,
        is_builtin_model,
        load_custom_models,
    )
    from core.model_discovery import DISCOVERABLE
    from db.ops import get_custom_models, get_default_model

    load_custom_models(await get_custom_models(session))
    return ModelCatalog(
        default=await get_default_model(session),
        available=[
            ModelSpec(
                id=m.id,
                label=m.label,
                provider=m.provider,
                builtin=is_builtin_model(m.id),
                context_window=m.context_window,
            )
            for m in available_models()
        ],
        providers=sorted(KNOWN_PROVIDERS),
        discoverable_providers=sorted(KNOWN_PROVIDERS & DISCOVERABLE),
    )
