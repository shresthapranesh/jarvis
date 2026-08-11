"""The `conversations` SDK category — the agent's way back into past chats.

The SDK reads over its own read-only sqlite connection, so these drive the
real functions against a real database rather than mocking the query out.
"""

from __future__ import annotations

from pathlib import Path

import pytest


async def _seed(database) -> dict[str, str]:
    """Two project siblings, one unrelated chat, one incognito, one bot thread."""
    from db import ops
    from db.models import Project

    ids: dict[str, str] = {}
    async with database.session() as s:
        proj = Project(id="p1", name="Ledger", instructions="", memory="")
        s.add(proj)
        await s.commit()

        siblings = [
            ("Invoice reconciliation", "we settled on idempotency keys for the invoice retry path"),
            ("Ledger schema", "the postings table needs a composite index on (account_id, posted_at)"),
        ]
        for title, body in siblings:
            conv = await ops.create_conversation(s, "m", title, project_id="p1")
            ids[title] = conv.id
            await ops.add_message(s, conv.id, "user", body)
            await ops.add_message(s, conv.id, "assistant", f"noted: {body}")

        other = await ops.create_conversation(s, "m", "Unrelated", project_id=None)
        ids["other"] = other.id
        await ops.add_message(s, other.id, "user", "idempotency keys came up here too")

        ghost = await ops.create_conversation(s, "m", "Incognito", project_id="p1", ephemeral=True)
        ids["ghost"] = ghost.id
        await ops.add_message(s, ghost.id, "user", "idempotency keys, but off the record")

        bot = await ops.create_conversation(s, "m", "Telegram", surface="telegram")
        ids["bot"] = bot.id
        await ops.add_message(s, bot.id, "user", "idempotency keys over telegram")
    return ids


@pytest.fixture
def sdk(work_dir: Path):
    """The SDK module with its module-level scope reset between tests."""
    from tools import sdk

    yield sdk
    sdk.set_conversation(None)
    sdk.set_project(None)


async def test_search_finds_project_siblings(database, sdk):
    ids = await _seed(database)
    sdk.set_conversation(ids["Ledger schema"])
    sdk.set_project("p1")

    hits = sdk.search_conversations("idempotency keys")

    found = {h["conversation_id"] for h in hits}
    assert found == {ids["Invoice reconciliation"]}, (
        "the sibling that discussed it, and nothing else: the current conversation, "
        "incognito, out-of-project and bot threads are all excluded"
    )
    assert "idempotency" in hits[0]["snippet"]
    assert hits[0]["matches"] == 2, "user message + assistant echo"


async def test_search_is_stemmed_not_substring(database, sdk):
    """Proof the FTS index is what answers — 'posted' is nowhere in the text."""
    ids = await _seed(database)

    found = {h["conversation_id"] for h in sdk.search_conversations("posted")}

    assert ids["Ledger schema"] in found, "porter stemming matches 'postings'"


async def test_search_unscoped_spans_projects_and_returns_snippets(database, sdk):
    ids = await _seed(database)

    hits = sdk.search_conversations("idempotency")

    found = {h["conversation_id"] for h in hits}
    assert ids["other"] in found, "no project scope — search everything on the surface"
    assert ids["ghost"] not in found
    assert all(h["snippet"] for h in hits), "every message hit carries an excerpt"
    assert all(h["matches"] >= 1 for h in hits)


async def test_search_matches_titles_with_no_body_hit(database, sdk):
    ids = await _seed(database)

    hits = {h["conversation_id"]: h for h in sdk.search_conversations("Ledger schema")}

    assert ids["Ledger schema"] in hits
    assert hits[ids["Ledger schema"]]["title"] == "Ledger schema"


async def test_search_survives_fts_syntax_in_the_query(database, sdk):
    """User text goes nowhere near MATCH raw — quotes/operators must not raise."""
    await _seed(database)

    for query in ['"unterminated', "index*", "a OR NOT b", "path/to:thing", "-x", "50%"]:
        sdk.search_conversations(query)


async def test_search_falls_back_when_fts_is_unavailable(database, sdk, monkeypatch):
    """A SQLite built without FTS5 has no mirror table — search still answers."""
    ids = await _seed(database)
    monkeypatch.setattr(sdk, "_has_fts", lambda conn, name: False)

    found = {h["conversation_id"] for h in sdk.search_conversations("idempotency")}

    assert ids["Invoice reconciliation"] in found
    assert ids["ghost"] not in found, "the scope filter applies on both paths"


async def test_list_conversations_is_the_project_roster(database, sdk):
    ids = await _seed(database)
    sdk.set_project("p1")

    rows = sdk.list_conversations()

    assert {r["conversation_id"] for r in rows} == {
        ids["Invoice reconciliation"], ids["Ledger schema"]
    }, "project members only — not the incognito one, not other projects' chats"
    assert all(r["messages"] == 2 for r in rows)
    assert all(r["last_message_at"] for r in rows)


async def test_list_conversations_refuses_outside_a_project(database, sdk):
    """Without a project there is no bounded set to list, only the user's whole
    history — that is a search, not a browse."""
    await _seed(database)
    sdk.set_conversation("whatever")

    with pytest.raises(RuntimeError, match="search_conversations"):
        sdk.list_conversations()


async def test_read_conversation_defaults_to_current_and_truncates(database, sdk):
    ids = await _seed(database)
    sdk.set_conversation(ids["Ledger schema"])

    out = sdk.read_conversation(max_chars=10)

    assert out["conversation_id"] == ids["Ledger schema"]
    assert out["title"] == "Ledger schema"
    assert out["total_messages"] == 2
    assert [m["role"] for m in out["messages"]] == ["user", "assistant"], "oldest first"
    assert out["messages"][0]["content"].endswith("… [truncated]")


async def test_read_conversation_errors_are_explicit(database, sdk):
    await _seed(database)

    with pytest.raises(RuntimeError):
        sdk.read_conversation()
    with pytest.raises(LookupError):
        sdk.read_conversation("nope")
