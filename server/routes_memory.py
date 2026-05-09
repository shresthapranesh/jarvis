"""Memory consolidation + edit endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.memory_consolidation import (
    _MEMORY_KEY,
    _MEMORY_NS,
    _migrate_legacy_key,
    consolidate_memory,
)
from core.model_catalog import DEFAULT_MODEL, is_valid_model
from core.schemas import MemoryUpdate
from core.state import get_store

router = APIRouter()


@router.post("/consolidate-memory")
async def trigger_consolidation(model: str | None = None) -> JSONResponse:
    """Manually trigger a memory consolidation run."""
    model_id = model or DEFAULT_MODEL
    if not is_valid_model(model_id):
        return JSONResponse({"error": f"unknown model {model_id!r}; GET /models for the catalog"}, status_code=400)
    try:
        result = await consolidate_memory(get_store(), model_id=model_id)
        return JSONResponse({"ok": True, "result": result})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/memory")
async def get_memory() -> JSONResponse:
    """Return the current AGENTS.md memory content."""
    store = get_store()
    await _migrate_legacy_key(store)
    item = await store.aget(_MEMORY_NS, _MEMORY_KEY)
    if item is None:
        return JSONResponse({"content": "", "exists": False, "modified_at": None})
    raw = item.value.get("content", "")
    if isinstance(raw, list):
        raw = "\n".join(raw)
    return JSONResponse({
        "content": raw,
        "exists": True,
        "modified_at": item.value.get("modified_at"),
    })


@router.put("/memory")
async def update_memory(body: MemoryUpdate) -> JSONResponse:
    """Overwrite AGENTS.md with the supplied content."""
    store = get_store()
    await _migrate_legacy_key(store)
    now_iso = datetime.now(timezone.utc).isoformat()
    existing = await store.aget(_MEMORY_NS, _MEMORY_KEY)
    created_at = (existing.value.get("created_at") if existing else None) or now_iso
    await store.aput(_MEMORY_NS, _MEMORY_KEY, {
        "content": body.content,
        "encoding": "utf-8",
        "created_at": created_at,
        "modified_at": now_iso,
    })
    return JSONResponse({"content": body.content, "exists": True, "modified_at": now_iso})
