"""Input type for referencing a staged upload from `POST /uploads`."""

from __future__ import annotations

import strawberry


@strawberry.input
class UploadReferenceInput:
    """Reference to a file previously staged via `POST /uploads`."""
    upload_id: str
