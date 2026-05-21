"""Document raw download — the only document endpoint not migrated to GraphQL.

Document list/delete moved to GraphQL. Raw download stays REST because it
returns binary content with mime type + filename headers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from db.ops import get_document


router = APIRouter()


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
