"""The `config_settings` table as an administrable surface.

`main.py config set/get/list/delete` writes this table from a terminal. That
process exits immediately, so a CLI write only ever lands in the DB and the
running server picks it up whenever it next reads the key — or, for the keys
read once at startup, not until a restart. Doing the same write *inside* the
server can do better, and has to: several keys are cached in process memory
(the compiled agent graphs, the tool-policy cache, the MCP client, the embedder
override), so a plain row write would be silently ignored by the very process
serving the page that made it.

Two things live here:

* `KNOWN_SETTINGS` — what each key is for, and who owns it. A generic key/value
  editor over a table that also holds `tools.policy` (a JSON blob written by a
  dedicated tab) is a footgun; marking those `managed_by` lets the UI show them
  read-only and point at the tab that edits them safely.
* `apply_setting` — the in-process side effects, so a write through the API
  takes effect on the next run rather than the next restart.

Unknown keys are *allowed*: the table is open-ended by design (the CLI never
validated keys either), and refusing them would make the UI strictly less
capable than the command it replaces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    description: str
    # Non-empty when another Settings tab owns this key. The generic editor
    # renders those read-only — hand-editing a serialized policy blob is how
    # you get a config the owning tab then overwrites without warning.
    managed_by: str = ""
    # text | csv | json | select — a hint for the input widget, nothing more.
    kind: str = "text"
    choices: tuple[str, ...] = ()
    placeholder: str = ""
    # True when no amount of in-process work can apply the change live.
    restart_required: bool = False


KNOWN_SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="default.model",
        label="Default model",
        description="Model used when a run doesn't name one. Set it from the Models tab to pick from the catalog.",
        managed_by="Models",
    ),
    SettingSpec(
        key="embedding.model",
        label="Embedding model",
        description="Gemini embedding model for document indexing and vector memory. Needs GOOGLE_API_KEY. Applied immediately; already-embedded content keeps its old vectors.",
        placeholder="models/gemini-embedding-001",
    ),
    SettingSpec(
        key="scheduler.timezone",
        label="Scheduler timezone",
        description="Zone cron expressions are interpreted in. Falls back to JARVIS_TIMEZONE, then the machine's local zone.",
        placeholder="America/New_York",
        restart_required=True,
    ),
    SettingSpec(
        key="telegram.allowed_users",
        label="Telegram allowlist",
        description="Comma-separated Telegram user IDs that may talk to the bot. Empty rejects everyone. Get an id from @userinfobot.",
        kind="csv",
        placeholder="123456789,987654321",
    ),
    SettingSpec(
        key="discord.allowed_users",
        label="Discord allowlist",
        description="Comma-separated Discord user IDs that may talk to the bot. Empty rejects everyone. Enable Developer Mode → right-click a user → Copy User ID.",
        kind="csv",
        placeholder="123456789,987654321",
    ),
    SettingSpec(
        key="approval.required_actions",
        label="Actions requiring approval",
        description="Which of the agent's destructive writes must be approved by a human first. `all`, `none`, or a comma-separated list of action names. Unset means none.",
        kind="csv",
        placeholder="all",
    ),
    SettingSpec(
        key="browser.cdp_url",
        label="Browser CDP endpoint",
        description="DevTools endpoint `read(url, browser=True)` attaches to. A browser is launched here on demand if nothing is listening; point it elsewhere to use one on another machine.",
        placeholder="http://127.0.0.1:9222",
    ),
    SettingSpec(
        key="browser.executable",
        label="Browser executable",
        description="Path to the Chromium-based browser to launch — Chrome, Brave, Edge, Chromium, Vivaldi. Unset probes the usual install locations. Only the launch path uses this; attaching works with whatever is already running.",
        placeholder="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ),
    SettingSpec(
        key="browser.profile_dir",
        label="Browser profile directory",
        description="Dedicated user-data-dir for that browser (default: <work_dir>/browser-profile). Log in here once and the cookies persist across runs. Never point this at your everyday profile: Chromium refuses remote debugging on the default profile, and a separate one keeps your other sessions out of the agent's reach.",
        placeholder="~/.jarvis/browser-profile",
    ),
    SettingSpec(
        key="models.custom",
        label="Custom models",
        description="Models added at runtime, as JSON.",
        managed_by="Models",
        kind="json",
    ),
    SettingSpec(
        key="tools.policy",
        label="Tool policy",
        description="Per-tool enabled / requires-approval overrides, as JSON. Only non-default entries are stored.",
        managed_by="Tools",
        kind="json",
    ),
    SettingSpec(
        key="mcp.servers",
        label="MCP servers",
        description="MCP server connection configs added through the UI, as JSON. Merged over env and mcp.json.",
        managed_by="MCP servers",
        kind="json",
    ),
    SettingSpec(
        key="mcp.load_modes",
        label="MCP load modes",
        description="Per-server always/lazy overrides, as JSON. Kept apart from the configs so flipping a mode doesn't snapshot an env-defined server into the DB.",
        managed_by="MCP servers",
        kind="json",
    ),
    SettingSpec(
        key="migration.artifact_message_ids",
        label="Artifact attribution migration",
        description="A one-time marker written by the server after backfilling artifact→message links. Nothing reads it but the migration.",
        managed_by="the server",
    ),
    SettingSpec(
        key="mcp.default_load_mode",
        label="MCP default load mode",
        description="Mode for servers that don't declare one. `always` binds their schemas to every LLM call; `lazy` keeps them out of the prompt until the agent asks.",
        managed_by="MCP servers",
        kind="select",
        choices=("always", "lazy"),
    ),
)

_BY_KEY = {s.key: s for s in KNOWN_SETTINGS}


def spec_for(key: str) -> SettingSpec | None:
    return _BY_KEY.get(key)


def is_managed(key: str) -> bool:
    spec = _BY_KEY.get(key)
    return bool(spec and spec.managed_by)


def validate(key: str, value: str) -> None:
    """Reject writes that are certainly wrong. Raises ValueError.

    Deliberately thin: this table is open-ended, and the CLI accepted anything.
    We only catch what has a single right answer.
    """
    key = key.strip()
    if not key:
        raise ValueError("key is empty")
    if any(c.isspace() for c in key):
        raise ValueError(f"key {key!r} contains whitespace")
    spec = _BY_KEY.get(key)
    if spec is None:
        return
    if spec.kind == "json" and value.strip():
        import json

        try:
            json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{key} must be valid JSON: {exc}") from exc
    if spec.kind == "select" and value.strip() and value.strip() not in spec.choices:
        raise ValueError(f"{key} must be one of: {', '.join(spec.choices)}")


async def apply_setting(session, key: str) -> str:
    """Re-read `key` and push it into this process. Returns a human-readable note.

    Called after every write and delete, so the effect of "delete the row" is
    the same code path as "set it back to the default" — the caches this drops
    have no idea a row was involved.

    Best-effort: a failure here must not roll back a write that already
    committed. The note it returns is what the UI shows.
    """
    from db.ops import get_setting

    value = await get_setting(session, key)

    try:
        if key == "embedding.model":
            from core.doc_index import configure_embedding_model

            configure_embedding_model(value)
            return "Applied. New embeddings use this model; existing vectors are unchanged."

        if key == "scheduler.timezone":
            # APScheduler will not reconfigure a running scheduler, so this is
            # the one key we cannot honestly claim to have applied.
            return "Saved. Takes effect when the server restarts."

        if key in ("models.custom", "default.model"):
            from core.model_catalog import load_custom_models
            from db.ops import get_custom_models

            load_custom_models(await get_custom_models(session))
            return "Applied."

        if key == "tools.policy":
            from core.agents import invalidate_agent_cache
            from core.tool_policy import invalidate_cache

            invalidate_cache()
            invalidate_agent_cache()
            return "Applied. New runs use the updated policy."

        if key.startswith("mcp."):
            from core.mcp import (
                get_mcp_manager,
                get_mcp_load_modes_from_db,
                get_mcp_servers_from_db,
                load_mcp_server_configs_with_db,
                sync_default_load_mode_from_db,
            )

            await sync_default_load_mode_from_db(session)
            merged = load_mcp_server_configs_with_db(
                db_cfg=await get_mcp_servers_from_db(session),
                load_modes=await get_mcp_load_modes_from_db(session),
            )
            await get_mcp_manager().reload(merged)
            return f"Applied. Reconnected {len(merged)} MCP server(s)."
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("apply_setting(%s) failed: %s", key, exc)
        return f"Saved, but applying it live failed: {exc}"

    # Everything else is read from the DB at the point of use (allowlists, the
    # approval action set), so the write is already live.
    return "Applied."
