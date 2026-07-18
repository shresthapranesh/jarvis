"""Artifact raw download — the only artifact endpoint not migrated to GraphQL."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_config
from db import get_session
from db.ops import get_artifact


router = APIRouter()


@router.get("/artifacts/{artifact_id}/raw", response_model=None)
async def raw_endpoint(
    artifact_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    art = await get_artifact(session, artifact_id)
    if art is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = Path(get_config().artifacts_dir) / f"{artifact_id}.md"
    if not path.exists():
        return JSONResponse({"error": "file missing"}, status_code=404)
    headers = {
        "Content-Disposition": f'attachment; filename="{art.title or art.id}.md"',
    }
    return PlainTextResponse(path.read_text(encoding="utf-8"), headers=headers)
