"""GraphQL types for the model catalog."""

from __future__ import annotations

import strawberry


@strawberry.type
class ModelSpec:
    id: str
    label: str
    provider: str


@strawberry.type
class ModelCatalog:
    default: str
    available: list[ModelSpec]
