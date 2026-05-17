"""Notification channel CRUD — `/notification-channels`.

Channels are referenced by id from automation/workflow `notifications` JSON;
delete is blocked while any automation or workflow still references the channel.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.schemas import NotificationChannelCreate, NotificationChannelUpdate
from db import get_session
from db.ops import (
    create_notification_channel,
    delete_notification_channel,
    get_notification_channel,
    list_notification_channels,
    list_references_to_channel,
    update_notification_channel,
)


router = APIRouter()


def _serialize(ch) -> dict:
    return {
        "id": ch.id,
        "name": ch.name,
        "type": ch.type,
        "target": ch.target,
        "created_at": ch.created_at.isoformat(),
        "updated_at": ch.updated_at.isoformat(),
    }


@router.get("/notification-channels")
async def list_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    rows = await list_notification_channels(session)
    return JSONResponse([_serialize(c) for c in rows])


@router.post("/notification-channels")
async def create_endpoint(
    body: NotificationChannelCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    if not body.target.strip():
        return JSONResponse({"error": "target required"}, status_code=400)
    if not body.name.strip():
        return JSONResponse({"error": "name required"}, status_code=400)
    ch = await create_notification_channel(
        session, name=body.name.strip(), type=body.type, target=body.target.strip(),
    )
    return JSONResponse(_serialize(ch))


@router.put("/notification-channels/{channel_id}")
async def update_endpoint(
    channel_id: str,
    body: NotificationChannelUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    existing = await get_notification_channel(session, channel_id)
    if existing is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if body.target is not None and not body.target.strip():
        return JSONResponse({"error": "target required"}, status_code=400)
    if body.name is not None and not body.name.strip():
        return JSONResponse({"error": "name required"}, status_code=400)
    ch = await update_notification_channel(
        session, channel_id,
        name=body.name.strip() if body.name is not None else None,
        type=body.type,
        target=body.target.strip() if body.target is not None else None,
    )
    assert ch is not None
    return JSONResponse(_serialize(ch))


@router.delete("/notification-channels/{channel_id}")
async def delete_endpoint(
    channel_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    existing = await get_notification_channel(session, channel_id)
    if existing is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    refs = await list_references_to_channel(session, channel_id)
    if refs:
        return JSONResponse(
            {"error": "channel in use", "references": refs}, status_code=409,
        )
    await delete_notification_channel(session, channel_id)
    return JSONResponse({"ok": True})
