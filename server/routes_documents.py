"""Document endpoints — list per conversation, delete, and download raw bytes."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from db.ops import (
    delete_document as db_delete_document,
    get_document,
    list_documents,
)


router = APIRouter()


def _serialize(doc) -> dict:
    return {
        "id": doc.id,
        "conversation_id": doc.conversation_id,
        "message_id": doc.message_id,
        "filename": doc.filename,
        "mime_type": doc.mime_type,
        "size": doc.size,
        "created_at": doc.created_at.isoformat(),
    }


@router.get("/conversations/{conv_id}/documents")
async def list_endpoint(
    conv_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    rows = await list_documents(session, conv_id)
    return JSONResponse([_serialize(d) for d in rows])


@router.delete("/documents/{doc_id}")
async def delete_endpoint(
    doc_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    doc = await db_delete_document(session, doc_id)
    if doc is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        Path(doc.path).unlink(missing_ok=True)
    except OSError:
        pass
    return JSONResponse({"ok": True})


@router.get("/documents/{doc_id}/raw", response_model=None)
async def raw_endpoint(
    doc_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    doc = await get_document(session, doc_id)
    if doc is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = Path(doc.path)
    if not path.exists():
        return JSONResponse({"error": "file missing"}, status_code=404)
    return FileResponse(
        path=str(path),
        media_type=doc.mime_type or "application/octet-stream",
        filename=doc.filename,
    )
