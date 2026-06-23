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
from core.memory_store import embed_for_storage, upsert_memory
from core.model_catalog import DEFAULT_MODEL, is_valid_model
from core.state import get_store
from db.models import Memory as MemoryModel
from db.ops import create_memory, delete_memory, update_memory_item

from ..types.memory import Memory, MemoryItem


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

    @strawberry.mutation
    async def add_memory(
        self, info: strawberry.Info, text: str, kind: str = "fact"
    ) -> MemoryItem:
        """Add a discrete memory item (embeds + dedups when an embedder exists)."""
        text = text.strip()
        if not text:
            raise ValueError("memory text is empty")
        k = kind if kind in ("core", "fact") else "fact"
        session = info.context["session"]
        mem_id = await upsert_memory(text, k)
        if mem_id is None:
            # Keyless: still persist the item, just without an embedding.
            mem = await create_memory(session, text=text, kind=k, embedding=None)
        else:
            mem = await session.get(MemoryModel, mem_id)
        return MemoryItem.from_db(mem)

    @strawberry.mutation
    async def update_memory_item(
        self, info: strawberry.Info, id: str, text: str, kind: str | None = None
    ) -> MemoryItem:
        text = text.strip()
        if not text:
            raise ValueError("memory text is empty")
        session = info.context["session"]
        embedding = await embed_for_storage(text)
        k = kind if kind in ("core", "fact") else None
        mem = await update_memory_item(session, id, text=text, kind=k, embedding=embedding)
        if mem is None:
            raise ValueError("memory not found")
        return MemoryItem.from_db(mem)

    @strawberry.mutation
    async def delete_memory(self, info: strawberry.Info, id: str) -> bool:
        session = info.context["session"]
        return await delete_memory(session, id)
