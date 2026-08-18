"""Artifact + Document mutations — update/delete artifact, delete document, restore version."""

from __future__ import annotations

from pathlib import Path

import strawberry
from strawberry import relay

from core.artifact_storage import artifact_path, version_path
from db.ops import (
    create_artifact_version,
    delete_artifact as db_delete_artifact,
    delete_document as db_delete_document,
    get_artifact,
    get_artifact_version,
    get_latest_artifact_version_number,
    update_artifact as db_update_artifact,
)

from ..types.artifact import Artifact, ArtifactVersion


@strawberry.type
class ArtifactMutation:
    @strawberry.mutation
    async def update_artifact(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        title: str | None = None,
        content: str | None = None,
    ) -> Artifact:
        session = info.context["session"]
        art = await get_artifact(session, id.node_id)
        if art is None:
            raise ValueError("artifact not found")
        if title is not None:
            await db_update_artifact(session, id.node_id, title=title)
        if content is not None:
            if art.kind != "markdown":
                raise ValueError(
                    "binary artifacts can't be edited inline — recreate via write_artifact(file_path=...)"
                )
            # Version the old content before overwriting, mirroring write_artifact tool
            live_path = artifact_path(id.node_id, ".md")
            latest = await get_latest_artifact_version_number(session, id.node_id)
            if latest == 0 and live_path.exists():
                # Migrate existing file without history as v1
                v1_path = version_path(id.node_id, 1, ".md")
                try:
                    if not v1_path.exists():
                        v1_path.write_text(live_path.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception:
                    pass
                await create_artifact_version(session, id.node_id, art.title, str(v1_path), 1)
                latest = 1
            live_path.write_text(content, encoding="utf-8")
            # Create new version file
            new_ver = latest + 1
            ver_path = version_path(id.node_id, new_ver, ".md")
            try:
                ver_path.write_text(content, encoding="utf-8")
            except Exception:
                pass
            await create_artifact_version(session, id.node_id, title or art.title, str(ver_path), new_ver)
            await db_update_artifact(session, id.node_id)  # bump updated_at

        updated = await get_artifact(session, id.node_id)
        assert updated is not None
        return Artifact.from_db(updated)

    @strawberry.mutation
    async def restore_artifact_version(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        version: int,
    ) -> Artifact:
        session = info.context["session"]
        art = await get_artifact(session, id.node_id)
        if art is None:
            raise ValueError("artifact not found")
        ver = await get_artifact_version(session, id.node_id, version)
        if ver is None:
            raise ValueError(f"version {version} not found for artifact {id.node_id}")
        # Byte-level copy — works uniformly for markdown and binary kinds alike.
        try:
            data = Path(ver.filename).read_bytes()
        except Exception as exc:
            raise ValueError(f"failed to read version file: {exc}")

        ext = Path(ver.filename).suffix or Path(art.filename).suffix or ".bin"
        live_path = artifact_path(id.node_id, ext)
        latest = await get_latest_artifact_version_number(session, id.node_id)
        live_path.write_bytes(data)
        new_ver = latest + 1
        ver_path = version_path(id.node_id, new_ver, ext)
        try:
            ver_path.write_bytes(data)
        except Exception:
            pass
        await create_artifact_version(session, id.node_id, ver.title, str(ver_path), new_ver)
        await db_update_artifact(session, id.node_id, title=ver.title, filename=str(live_path))

        updated = await get_artifact(session, id.node_id)
        assert updated is not None
        return Artifact.from_db(updated)

    @strawberry.mutation
    async def delete_artifact(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> bool:
        session = info.context["session"]
        art = await get_artifact(session, id.node_id)
        if art is None:
            raise ValueError("artifact not found")
        # db_delete_artifact already cleans up the live file + all version files.
        await db_delete_artifact(session, id.node_id)
        return True

    @strawberry.mutation
    async def delete_document(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> bool:
        session = info.context["session"]
        doc = await db_delete_document(session, id.node_id)
        if doc is None:
            raise ValueError("document not found")
        try:
            Path(doc.path).unlink(missing_ok=True)
        except OSError:
            pass
        return True
