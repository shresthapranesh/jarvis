"""Artifact raw download — the only artifact endpoint not migrated to GraphQL."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from db.ops import get_artifact

_INLINE_KINDS = {"audio", "video", "image"}

router = APIRouter()


@router.get("/artifacts/{artifact_id}/raw", response_model=None)
async def raw_endpoint(
    artifact_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    art = await get_artifact(session, artifact_id)
    if art is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = Path(art.filename)
    if not path.exists():
        return JSONResponse({"error": "file missing"}, status_code=404)
    media_type = art.mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=f"{art.title or art.id}{path.suffix}",
        content_disposition_type="inline" if art.kind in _INLINE_KINDS else "attachment",
    )
