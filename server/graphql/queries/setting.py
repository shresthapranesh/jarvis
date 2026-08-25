"""Reads over `config_settings` — the query side of `main.py config get/list`."""

from __future__ import annotations

import strawberry

from db.ops import get_setting, list_settings

from ..types.setting import Setting, merge_inventory


@strawberry.type
class SettingQuery:
    @strawberry.field
    async def settings(self, info: strawberry.Info) -> list[Setting]:
        """Every stored setting, plus the known keys that aren't set yet."""
        session = info.context["session"]
        return merge_inventory(await list_settings(session))

    @strawberry.field
    async def setting(self, info: strawberry.Info, key: str) -> Setting:
        """One key. Returns an `isSet: false` row rather than null when absent,
        so the caller gets the key's guidance either way."""
        session = info.context["session"]
        value = await get_setting(session, key)
        if value is None:
            return Setting.unset(key)
        rows = await list_settings(session)
        row = next((r for r in rows if r.key == key), None)
        return Setting.from_db(row) if row else Setting.unset(key)
