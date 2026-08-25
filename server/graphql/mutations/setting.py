"""Writes over `config_settings` — the mutation side of `config set/delete`.

Unlike the CLI, these run *inside* the server, so they also push the change
into this process (`core.settings_admin.apply_setting`). Without that a write
would land in the DB and be ignored by the caches the same process is holding —
the compiled agent graphs, the tool-policy cache, the MCP client.
"""

from __future__ import annotations

import strawberry

from core.settings_admin import apply_setting, is_managed, validate
from db.ops import delete_setting, list_settings, set_setting

from ..types.setting import Setting, merge_inventory


@strawberry.type
class SettingWriteResult:
    """The written row, the refreshed list, and what applying it actually did."""

    setting: Setting
    settings: list[Setting]
    note: str


async def _result(session, key: str, note: str) -> SettingWriteResult:
    settings = merge_inventory(await list_settings(session))
    row = next((s for s in settings if s.key == key), Setting.unset(key))
    return SettingWriteResult(setting=row, settings=settings, note=note)


@strawberry.type
class SettingMutation:
    @strawberry.mutation
    async def set_setting(
        self,
        info: strawberry.Info,
        key: str,
        value: str,
        allow_managed: bool = False,
    ) -> SettingWriteResult:
        """Write one config key.

        Keys owned by a dedicated tab (`tools.policy`, `mcp.servers`, …) are
        refused unless `allowManaged`. They hold serialized state that their
        own tab rewrites wholesale, so a hand edit here is silently discarded
        the next time that tab writes — better to say so than to accept it.
        """
        key = key.strip()
        validate(key, value)
        if is_managed(key) and not allow_managed:
            raise ValueError(
                f"{key} is managed by another Settings tab; edit it there, "
                "or pass allowManaged: true to override."
            )
        session = info.context["session"]
        await set_setting(session, key, value)
        note = await apply_setting(session, key)
        return await _result(session, key, note)

    @strawberry.mutation
    async def delete_setting(
        self,
        info: strawberry.Info,
        key: str,
        allow_managed: bool = False,
    ) -> SettingWriteResult:
        """Remove one config key, reverting it to its built-in default."""
        key = key.strip()
        if is_managed(key) and not allow_managed:
            raise ValueError(
                f"{key} is managed by another Settings tab; edit it there, "
                "or pass allowManaged: true to override."
            )
        session = info.context["session"]
        deleted = await delete_setting(session, key)
        if not deleted:
            return await _result(session, key, f"Not set: {key}")
        # Same path as a write: the caches being dropped never knew a row was
        # involved, only that the effective value changed.
        note = await apply_setting(session, key)
        return await _result(session, key, f"Deleted. {note}")
