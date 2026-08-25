"""The `config_settings` table over GraphQL — the UI counterpart of `main.py config *`.

Three things here are not obvious from the resolvers and are exactly what a UI
gets wrong:

1. **Unset known keys are still listed.** A page that showed only stored rows
   would never mention the Telegram allowlist exists, which is the thing the
   CLI made you read the docs for.
2. **Managed keys are refused by default.** `tools.policy` / `mcp.servers` hold
   serialized state their own tab rewrites wholesale, so accepting a hand edit
   here would silently lose it on that tab's next write.
3. **A write applies in-process.** The CLI writes from a process that then
   exits; these resolvers run inside the server, where a plain row write would
   be ignored by the caches the same process is holding.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

SETTING_FIELDS = "{ key value isSet managedBy known restartRequired kind choices }"


def _context(session):
    from server.graphql.extensions import SESSION_LOCK_KEY

    return {"session": session, SESSION_LOCK_KEY: asyncio.Lock()}


async def _exec(query: str, variables: dict[str, Any] | None = None) -> Any:
    from db import async_session
    from server.graphql.schema import schema

    async with async_session() as s:
        return await schema.execute(query, variable_values=variables, context_value=_context(s))


async def _settings() -> list[dict[str, Any]]:
    res = await _exec("{ settings %s }" % SETTING_FIELDS)
    assert not res.errors, res.errors
    return res.data["settings"]


def _by_key(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    row = next((r for r in rows if r["key"] == key), None)
    assert row is not None, f"{key} missing from the settings listing"
    return row


# ── Listing ──────────────────────────────────────────────────────────────────

async def test_known_keys_are_listed_before_anyone_sets_them(database):
    rows = await _settings()
    # A settings page has to advertise what is configurable, not only what is set.
    allowlist = _by_key(rows, "telegram.allowed_users")
    assert allowlist["isSet"] is False
    assert allowlist["value"] == ""
    assert allowlist["known"] is True


async def test_scheduler_timezone_is_flagged_restart_required(database):
    # APScheduler will not reconfigure a running scheduler, so this is the one
    # key the server cannot honestly claim to have applied.
    assert _by_key(await _settings(), "scheduler.timezone")["restartRequired"] is True


# ── Writes ───────────────────────────────────────────────────────────────────

async def test_set_get_delete_round_trips(database):
    from db import async_session, ops

    res = await _exec(
        "mutation($k: String!, $v: String!) { setSetting(key: $k, value: $v)"
        " { note settings %s } }" % SETTING_FIELDS,
        {"k": "telegram.allowed_users", "v": "123,456"},
    )
    assert not res.errors, res.errors
    rows = res.data["setSetting"]["settings"]
    # The mutation's own response must already reflect the write — the client
    # renders this, not a follow-up query.
    assert _by_key(rows, "telegram.allowed_users")["value"] == "123,456"
    assert _by_key(rows, "telegram.allowed_users")["isSet"] is True

    async with async_session() as s:
        assert await ops.get_setting(s, "telegram.allowed_users") == "123,456"

    res = await _exec(
        "mutation($k: String!) { deleteSetting(key: $k) { note settings %s } }" % SETTING_FIELDS,
        {"k": "telegram.allowed_users"},
    )
    assert not res.errors, res.errors
    assert _by_key(res.data["deleteSetting"]["settings"], "telegram.allowed_users")["isSet"] is False
    async with async_session() as s:
        assert await ops.get_setting(s, "telegram.allowed_users") is None


async def test_free_form_keys_are_allowed(database):
    # The table is open-ended by design; refusing unknown keys would make this
    # strictly less capable than the command it replaces.
    res = await _exec(
        "mutation($k: String!, $v: String!) { setSetting(key: $k, value: $v)"
        " { settings %s } }" % SETTING_FIELDS,
        {"k": "some.experimental.key", "v": "hello"},
    )
    assert not res.errors, res.errors
    row = _by_key(res.data["setSetting"]["settings"], "some.experimental.key")
    assert row["value"] == "hello"
    assert row["known"] is False


async def test_managed_keys_are_refused_without_override(database):
    res = await _exec(
        "mutation($k: String!, $v: String!) { setSetting(key: $k, value: $v) { note } }",
        {"k": "tools.policy", "v": "{}"},
    )
    assert res.errors, "a hand edit to a tab-owned key must not be accepted silently"
    assert "managed" in str(res.errors[0]).lower()

    res = await _exec(
        "mutation($k: String!, $v: String!) {"
        " setSetting(key: $k, value: $v, allowManaged: true) { settings %s } }" % SETTING_FIELDS,
        {"k": "tools.policy", "v": "{}"},
    )
    assert not res.errors, res.errors
    assert _by_key(res.data["setSetting"]["settings"], "tools.policy")["value"] == "{}"


async def test_json_keys_reject_invalid_json(database):
    res = await _exec(
        "mutation($k: String!, $v: String!) {"
        " setSetting(key: $k, value: $v, allowManaged: true) { note } }",
        {"k": "mcp.servers", "v": "{not json"},
    )
    assert res.errors
    assert "json" in str(res.errors[0]).lower()


async def test_select_keys_reject_values_outside_the_choice_set(database):
    res = await _exec(
        "mutation($k: String!, $v: String!) {"
        " setSetting(key: $k, value: $v, allowManaged: true) { note } }",
        {"k": "mcp.default_load_mode", "v": "sometimes"},
    )
    assert res.errors


async def test_keys_with_whitespace_are_rejected(database):
    res = await _exec(
        "mutation($k: String!, $v: String!) { setSetting(key: $k, value: $v) { note } }",
        {"k": "bad key", "v": "x"},
    )
    assert res.errors


# ── In-process application ───────────────────────────────────────────────────

async def test_embedding_model_write_applies_to_this_process(database):
    """The whole reason this exists as a resolver and not just a DB write."""
    from core import doc_index

    before = doc_index._embedding_model_override
    try:
        res = await _exec(
            "mutation($k: String!, $v: String!) { setSetting(key: $k, value: $v) { note } }",
            {"k": "embedding.model", "v": "models/test-embedding-1"},
        )
        assert not res.errors, res.errors
        assert doc_index._embedding_model_override == "models/test-embedding-1"

        res = await _exec(
            "mutation($k: String!) { deleteSetting(key: $k) { note } }",
            {"k": "embedding.model"},
        )
        assert not res.errors, res.errors
        # Delete goes through the same apply path — the cache has no idea a row
        # was involved, only that the effective value changed.
        assert doc_index._embedding_model_override is None
    finally:
        doc_index.configure_embedding_model(before)


async def test_tool_policy_write_drops_the_compiled_agent_cache(database):
    from core import agents

    sentinel = ("sentinel",)
    agents._cache[sentinel] = object()  # type: ignore[assignment]
    res = await _exec(
        "mutation($k: String!, $v: String!) {"
        " setSetting(key: $k, value: $v, allowManaged: true) { note } }",
        {"k": "tools.policy", "v": "{}"},
    )
    assert not res.errors, res.errors
    assert sentinel not in agents._cache


async def test_deleting_an_unset_key_is_not_an_error(database):
    res = await _exec(
        "mutation($k: String!) { deleteSetting(key: $k) { note } }",
        {"k": "never.set.this"},
    )
    assert not res.errors, res.errors
    assert "Not set" in res.data["deleteSetting"]["note"]
