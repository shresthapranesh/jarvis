"""The scheduled project-memory consolidation pass.

Drives `consolidate_project_memory` against a real DB + store with the LLM call
stubbed, so the gates (quiet period, minimum material), the mode split
(merge = add-only, rewrite = may delete), and the watermark arithmetic are all
exercised without a model.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import project_memory_consolidation as pmc


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed(project_id: str = "p1", memory: str = "", n: int = 4, age_minutes: float = 60.0):
    """A project with one conversation of `n` substantial messages, `age` old."""
    from db.engine import async_session
    from db.models import Conversation, Message, Project

    async with async_session() as s:
        s.add(Project(id=project_id, name="Ledger", instructions="", memory=memory))
        s.add(
            Conversation(
                id="c1",
                title="Schema work",
                model="stub:model",
                surface="web",
                project_id=project_id,
                ephemeral=False,
            )
        )
        await s.commit()

        base = _now() - timedelta(minutes=age_minutes)
        for i in range(n):
            s.add(
                Message(
                    id=f"m{i}",
                    conversation_id="c1",
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"Message {i}. " + ("Decided the ledger uses double-entry postings. " * 20),
                    created_at=base + timedelta(seconds=i),
                )
            )
        await s.commit()


def _stub_llm(monkeypatch, reply: str, calls: list | None = None):
    """Replace the model round-trip with a canned reply."""

    async def fake_resolve(model_id=None):
        return object(), "stub:model"

    async def fake_ask(llm, system, user):
        if calls is not None:
            calls.append({"system": system, "user": user})
        return reply

    monkeypatch.setattr(pmc, "_resolve_llm", fake_resolve)
    monkeypatch.setattr(pmc, "_ask", fake_ask)


async def _memory(project_id: str = "p1") -> str:
    from db.engine import async_session
    from db.models import Project

    async with async_session() as s:
        proj = await s.get(Project, project_id)
        return (proj.memory or "") if proj else ""


# ── gates ─────────────────────────────────────────────────────────────────────

async def test_skips_while_the_conversation_is_still_active(jarvis, monkeypatch):
    from core.state import get_store

    await _seed(age_minutes=2)
    _stub_llm(monkeypatch, "- should never be asked")
    result = await pmc.consolidate_project_memory(get_store(), "p1")
    assert result.startswith("skipped: active")
    assert await _memory() == ""


async def test_force_bypasses_the_quiet_gate(jarvis, monkeypatch):
    from core.state import get_store

    await _seed(age_minutes=2)
    _stub_llm(monkeypatch, "- Ledger uses double-entry postings")
    result = await pmc.consolidate_project_memory(get_store(), "p1", force=True)
    assert result.startswith("merge: added")
    assert "double-entry" in await _memory()


async def test_skips_trivial_new_material(jarvis, monkeypatch):
    from core.state import get_store
    from db.engine import async_session
    from db.models import Conversation, Message, Project

    async with async_session() as s:
        s.add(Project(id="p1", name="Ledger", instructions="", memory=""))
        s.add(Conversation(
                id="c1",
                title="Hi",
                model="stub:model",
                surface="web",
                project_id="p1",
                ephemeral=False,
            ))
        await s.commit()
        s.add(
            Message(
                id="m0",
                conversation_id="c1",
                role="user",
                content="thanks!",
                created_at=_now() - timedelta(hours=1),
            )
        )
        await s.commit()

    _stub_llm(monkeypatch, "- should never be asked")
    result = await pmc.consolidate_project_memory(get_store(), "p1")
    assert result.startswith("skipped: only")


async def test_no_new_messages_is_a_skip(jarvis, monkeypatch):
    from core.state import get_store
    from db.engine import async_session
    from db.models import Project

    async with async_session() as s:
        s.add(Project(id="p1", name="Ledger", instructions="", memory=""))
        await s.commit()

    _stub_llm(monkeypatch, "- nope")
    assert "no new messages" in await pmc.consolidate_project_memory(get_store(), "p1")


# ── merge mode ────────────────────────────────────────────────────────────────

async def test_merge_appends_and_advances_the_watermark(jarvis, monkeypatch):
    from core.state import get_store

    store = get_store()
    await _seed()
    _stub_llm(monkeypatch, "- Ledger uses double-entry postings")

    assert (await pmc.consolidate_project_memory(store, "p1")).startswith("merge: added")
    assert "double-entry" in await _memory()

    through, _ = await pmc._load_meta(store, "p1")
    assert through is not None

    # Second pass sees nothing new — the watermark moved past those messages.
    assert "no new messages" in await pmc.consolidate_project_memory(store, "p1")


async def test_merge_cannot_delete_or_reword_existing_lines(jarvis, monkeypatch):
    """Add-only is enforced in code, not trusted to the prompt."""
    from core.state import get_store

    existing = "- Backend is FastAPI + Strawberry\n- Frontend is React 19 with Relay"
    await _seed(memory=existing)
    # A misbehaving model: drops one existing line, restates another, adds one.
    _stub_llm(
        monkeypatch,
        "- Backend is FastAPI + Strawberry GraphQL\n- Ledger uses double-entry postings",
    )

    await pmc.consolidate_project_memory(get_store(), "p1")
    memory = await _memory()
    assert "- Backend is FastAPI + Strawberry" in memory  # preserved verbatim
    assert "- Frontend is React 19 with Relay" in memory  # not dropped despite absence
    assert "double-entry" in memory  # genuinely new line landed
    # The restatement is a near-match of a line already there, so it doesn't
    # double up. A *loose* paraphrase would clear the dedup threshold and
    # survive to be cleaned up by rewrite mode — merge deliberately never edits.
    assert memory.count("FastAPI") == 1


async def test_merge_no_update_still_advances_the_watermark(jarvis, monkeypatch):
    """A pass that finds nothing must not re-read the same messages forever."""
    from core.state import get_store

    store = get_store()
    await _seed()
    _stub_llm(monkeypatch, pmc._NO_UPDATE_MARKER)

    assert "nothing new" in await pmc.consolidate_project_memory(store, "p1")
    through, _ = await pmc._load_meta(store, "p1")
    assert through is not None
    assert await _memory() == ""


# ── rewrite mode ──────────────────────────────────────────────────────────────

async def test_rewrite_runs_when_memory_is_at_the_bullet_cap(jarvis, monkeypatch):
    from core.state import get_store

    crowded = "\n".join(f"- fact number {i} about the ledger service" for i in range(pmc._MAX_BULLETS))
    await _seed(memory=crowded)
    calls: list = []
    _stub_llm(monkeypatch, "- one surviving fact about the ledger service", calls)

    result = await pmc.consolidate_project_memory(get_store(), "p1")
    assert result.startswith("rewrite:")
    # Rewrite replaces wholesale — this is the only mode that may shrink memory.
    assert await _memory() == "- one surviving fact about the ledger service"
    assert "only pass allowed to remove things" in calls[0]["system"]


async def test_merge_overflow_escalates_to_rewrite(jarvis, monkeypatch):
    """A merge that would breach the cap hands off to the mode that can evict."""
    from core.state import get_store

    # One under the cap, so mode starts as merge...
    existing = "\n".join(f"- fact number {i} about the ledger" for i in range(pmc._MAX_BULLETS - 1))
    await _seed(memory=existing)
    # ...and the model proposes three new bullets, which would breach it.
    _stub_llm(
        monkeypatch,
        "- alpha postings are immutable once cleared\n"
        "- beta reconciliation runs nightly at 0200\n"
        "- gamma exports use the ISO 20022 schema",
    )

    # last_rewrite_at is unset, so make it recent to prove the escalation comes
    # from the overflow rather than from the rewrite interval being due.
    await pmc._save_meta(get_store(), "p1", None, _now())
    result = await pmc.consolidate_project_memory(get_store(), "p1")
    assert result.startswith("rewrite:")


# ── concurrency ───────────────────────────────────────────────────────────────

async def test_concurrent_write_aborts_without_advancing_the_watermark(jarvis, monkeypatch):
    """The agent writing mid-pass wins; the job retries next tick."""
    from core.state import get_store
    from db.engine import async_session
    from db.ops import update_project

    store = get_store()
    await _seed()

    async def fake_resolve(model_id=None):
        return object(), "stub:model"

    async def racing_ask(llm, system, user):
        # The in-band agent tool lands while the model call is in flight.
        async with async_session() as s:
            await update_project(s, "p1", memory="- written by the agent mid-pass")
        return "- Ledger uses double-entry postings"

    monkeypatch.setattr(pmc, "_resolve_llm", fake_resolve)
    monkeypatch.setattr(pmc, "_ask", racing_ask)

    result = await pmc.consolidate_project_memory(store, "p1")
    assert result.startswith("skipped: concurrent write")
    assert await _memory() == "- written by the agent mid-pass"

    through, _ = await pmc._load_meta(store, "p1")
    assert through is None  # not advanced — the next pass re-derives the fact


# ── material windowing ────────────────────────────────────────────────────────

async def test_watermark_only_advances_as_far_as_material_was_consumed(jarvis, monkeypatch):
    """A budget cut becomes a backlog, never a silent gap."""
    from core.state import get_store

    store = get_store()
    await _seed(n=10)
    monkeypatch.setattr(pmc, "_MATERIAL_BUDGET", 600)  # forces an early break
    _stub_llm(monkeypatch, pmc._NO_UPDATE_MARKER)

    await pmc.consolidate_project_memory(store, "p1")
    through, _ = await pmc._load_meta(store, "p1")

    from db.engine import async_session
    from db.ops import get_project_activity_since

    async with async_session() as s:
        remaining, _oldest, _newest, _chars = await get_project_activity_since(s, "p1", through)
    assert remaining > 0  # the tail is still queued for the next pass


async def test_sweep_reports_only_projects_it_touched(jarvis, monkeypatch):
    from core.state import get_store
    from db.engine import async_session
    from db.models import Project

    await _seed()
    async with async_session() as s:  # a second, idle project
        s.add(Project(id="p2", name="Idle", instructions="", memory=""))
        await s.commit()

    _stub_llm(monkeypatch, "- Ledger uses double-entry postings")
    summary = await pmc.consolidate_project_memories(get_store())
    assert "Ledger" in summary
    assert "Idle" not in summary
