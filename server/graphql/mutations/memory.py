"""Agent memory mutations."""

from __future__ import annotations

from datetime import datetime, timezone

import strawberry

from core.memory_consolidation import (
    _MEMORY_KEY,
    _MEMORY_NS,
    _migrate_legacy_key,
    consolidate_memory,
)
from core.model_catalog import DEFAULT_MODEL, is_valid_model
from core.state import get_store

from ..types.memory import Memory


@strawberry.type
class MemoryMutation:
    @strawberry.mutation
    async def update_memory(self, content: str) -> Memory:
        store = get_store()
        await _migrate_legacy_key(store)
        now_iso = datetime.now(timezone.utc).isoformat()
        existing = await store.aget(_MEMORY_NS, _MEMORY_KEY)
        created_at = (existing.value.get("created_at") if existing else None) or now_iso
        await store.aput(
            _MEMORY_NS,
            _MEMORY_KEY,
            {
                "content": content,
                "encoding": "utf-8",
                "created_at": created_at,
                "modified_at": now_iso,
            },
        )
        return Memory(content=content, exists=True, modified_at=now_iso)

    @strawberry.mutation
    async def consolidate_memory(self, model: str | None = None) -> str:
        model_id = model or DEFAULT_MODEL
        if not is_valid_model(model_id):
            raise ValueError(f"unknown model {model_id!r}; query `models` for the catalog")
        return await consolidate_memory(get_store(), model_id=model_id)
