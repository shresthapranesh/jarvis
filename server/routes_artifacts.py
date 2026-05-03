"""Artifact endpoints — list, fetch, edit, delete, and download deliverables."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_config
from db import get_session
from db.ops import (
    delete_artifact as db_delete_artifact,
    get_artifact,
    list_artifacts as db_list_artifacts,
    update_artifact,
)


router = APIRouter()


class ArtifactUpdate(BaseModel):
    title: str | None = None
    content: str | None = None


def _path_for(artifact_id: str) -> Path:
    cfg = get_config()
    cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return cfg.artifacts_dir / f"{artifact_id}.md"


def _serialize(art) -> dict:
    return {
        "id": art.id,
        "title": art.title,
        "kind": art.kind,
        "conversation_id": art.conversation_id,
        "message_id": art.message_id,
        "created_at": art.created_at.isoformat(),
        "updated_at": art.updated_at.isoformat(),
    }


@router.get("/artifacts")
async def list_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    conversation_id: str | None = None,
) -> JSONResponse:
    rows = await db_list_artifacts(session, conversation_id=conversation_id)
    return JSONResponse([_serialize(a) for a in rows])


@router.get("/artifacts/{artifact_id}")
async def get_endpoint(
    artifact_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    art = await get_artifact(session, artifact_id)
    if art is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = _path_for(artifact_id)
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    return JSONResponse({**_serialize(art), "content": content})


@router.patch("/artifacts/{artifact_id}")
async def patch_endpoint(
    artifact_id: str,
    body: ArtifactUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    art = await get_artifact(session, artifact_id)
    if art is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if body.title is not None:
        await update_artifact(session, artifact_id, title=body.title)
    if body.content is not None:
        _path_for(artifact_id).write_text(body.content, encoding="utf-8")
        await update_artifact(session, artifact_id)  # bump updated_at
    return JSONResponse({"ok": True})


@router.delete("/artifacts/{artifact_id}")
async def delete_endpoint(
    artifact_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    art = await get_artifact(session, artifact_id)
    if art is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = _path_for(artifact_id)
    if path.exists():
        path.unlink()
    await db_delete_artifact(session, artifact_id)
    return JSONResponse({"ok": True})


@router.get("/artifacts/{artifact_id}/raw", response_model=None)
async def raw_endpoint(
    artifact_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    art = await get_artifact(session, artifact_id)
    if art is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = _path_for(artifact_id)
    if not path.exists():
        return JSONResponse({"error": "file missing"}, status_code=404)
    headers = {
        "Content-Disposition": f'attachment; filename="{art.title or art.id}.md"',
    }
    return PlainTextResponse(path.read_text(encoding="utf-8"), headers=headers)
