"""Upload staging — POST /uploads accepts a multipart file, stores bytes in the
staging dir, and returns an opaque uploadId the client passes back to startTask.

This decouples bulk uploads from the chat mutation so large files don't bloat
GraphQL request bodies. Stale staged files are GC'd by a periodic scheduler job."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, UploadFile
from fastapi.responses import JSONResponse

from core.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MiB cap per file


@router.post("/uploads")
async def upload_file(file: UploadFile) -> JSONResponse:
    data = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(data) > _MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"error": f"upload exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MiB limit"},
            status_code=413,
        )

    upload_id = str(uuid4())
    cfg = get_config()
    cfg.staging_dir.mkdir(parents=True, exist_ok=True)

    bytes_path = cfg.staging_dir / upload_id
    meta_path = cfg.staging_dir / f"{upload_id}.meta.json"

    bytes_path.write_bytes(data)
    meta_path.write_text(json.dumps({
        "filename": file.filename or upload_id,
        "mime_type": file.content_type or "application/octet-stream",
        "size": len(data),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }))

    return JSONResponse({
        "uploadId": upload_id,
        "filename": file.filename or upload_id,
        "mimeType": file.content_type or "application/octet-stream",
        "size": len(data),
    })
