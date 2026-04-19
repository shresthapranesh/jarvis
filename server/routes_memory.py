"""Memory consolidation endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.memory_consolidation import _MEMORY_KEY, _MEMORY_NS, consolidate_memory
from core.model_catalog import DEFAULT_MODEL, is_valid_model
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
    item = await get_store().aget(_MEMORY_NS, _MEMORY_KEY)
    if item is None:
        return JSONResponse({"content": "", "exists": False})
    raw = item.value.get("content", "")
    if isinstance(raw, list):
        raw = "\n".join(raw)
    return JSONResponse({
        "content": raw,
        "exists": True,
        "modified_at": item.value.get("modified_at"),
    })
