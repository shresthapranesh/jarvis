"""Setting — one row of `config_settings`, the table the `config` CLI writes.

Not a Relay Node: nothing refetches a setting by global id, and the key is
already a stable primary key. It does carry a plain `id` so Relay normalizes
`setSetting`'s response onto the record the page is rendering — prefixed with
`setting:` because `id` alone is Relay's whole cache key, and a bare
`default.model` would be free to collide with some other type's id.
"""

from __future__ import annotations

from datetime import datetime

import strawberry

from core.settings_admin import KNOWN_SETTINGS, SettingSpec, spec_for
from db.models import ConfigSetting


@strawberry.type
class Setting:
    id: strawberry.ID
    key: str
    value: str
    updated_at: datetime | None
    # Whether a row actually exists. A known-but-unset key is listed too, so
    # the editor can show what is configurable rather than only what is set.
    is_set: bool
    label: str
    description: str
    # Non-empty when a dedicated Settings tab owns this key; the generic
    # editor renders those read-only and names the tab.
    managed_by: str
    kind: str
    choices: list[str]
    placeholder: str
    restart_required: bool
    # True when the key isn't in the known registry — free-form, no guidance.
    known: bool

    @classmethod
    def _from_parts(
        cls, key: str, value: str, updated_at: datetime | None, is_set: bool
    ) -> "Setting":
        spec: SettingSpec | None = spec_for(key)
        return cls(
            id=strawberry.ID(f"setting:{key}"),
            key=key,
            value=value,
            updated_at=updated_at,
            is_set=is_set,
            label=spec.label if spec else key,
            description=spec.description if spec else "",
            managed_by=spec.managed_by if spec else "",
            kind=spec.kind if spec else "text",
            choices=list(spec.choices) if spec else [],
            placeholder=spec.placeholder if spec else "",
            restart_required=spec.restart_required if spec else False,
            known=spec is not None,
        )

    @classmethod
    def from_db(cls, row: ConfigSetting) -> "Setting":
        return cls._from_parts(row.key, row.value, row.updated_at, True)

    @classmethod
    def unset(cls, key: str) -> "Setting":
        return cls._from_parts(key, "", None, False)


def merge_inventory(rows: list[ConfigSetting]) -> list[Setting]:
    """Stored rows ∪ every known key, so unset keys are still discoverable.

    A settings page that only listed what someone had already written would
    never tell you the allowlist exists — which is exactly the thing the CLI
    made you read the docs for.
    """
    stored = {row.key: Setting.from_db(row) for row in rows}
    out = list(stored.values())
    out.extend(Setting.unset(s.key) for s in KNOWN_SETTINGS if s.key not in stored)
    # Known keys first (registry order), then free-form ones alphabetically.
    order = {s.key: i for i, s in enumerate(KNOWN_SETTINGS)}
    return sorted(out, key=lambda s: (order.get(s.key, len(order)), s.key))
