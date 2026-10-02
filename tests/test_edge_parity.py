"""The Rust edge must answer exactly what the Python schema answers.

The edge (`edge/`) serves a growing slice of the GraphQL API and proxies the
rest to Python, so for every operation it claims, the two servers have to be
indistinguishable to the frontend: same JSON, same global ids, same timestamp
strings, same cursors. These tests seed one database, ask both servers the
frontend's *real* Relay operations (read from `frontend/src/__generated__`),
and diff the results.

The edge is started with its backend pointed at a closed port, so an operation
it proxies instead of answering comes back as a 502 — "served by the edge" is
asserted, not assumed.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from edge_support import _gid, _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture

ROOT = Path(__file__).resolve().parent.parent
GENERATED = ROOT / "frontend" / "src" / "__generated__"

# Every frontend query whose root fields the edge implements must be listed
# here, so porting a field can't silently route an operation nobody diffed.
PARITY_OPERATIONS = {
    "ConversationListQuery",
    "ConversationPageQuery",
    "ConversationPageRefetchQuery",
    "ProjectsQuery",
    "ProjectQuery",
    "ArtifactDetailQuery",
    "ArtifactListQuery",
    "DocumentListQuery",
    "AutomationRunsQuery",
    "BoardTasksQuery",
    "WorkflowListQuery",
    "WorkflowDetailQuery",
    "WorkflowRunsQuery",
    "WorkflowRunDetailQuery",
    "NotificationChannelsQuery",
    "SkillsQuery",
    "PendingApprovalsQuery",
    "MemoriesQuery",
    # Diffed against live runs in test_edge_runs.py.
    "RunningTasksQuery",
}


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def edge(database, work_dir: Path, edge_binary: Path):
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db") as client:
        yield client


def _ts(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
async def seeded(database) -> dict[str, str]:
    """A small database exercising every shape the slice has to reproduce."""
    from db import async_session
    from db.models import Conversation, Message, Project, Step

    async with async_session() as s:
        s.add(Project(
            id="p1", name="Thesis", description=None, instructions="cite sources",
            memory="- prefers APA", created_at=_ts(2026, 3, 1, 9), updated_at=_ts(2026, 3, 2, 9, 30, 0, 250),
        ))
        s.add(Project(id="p2", name="Empty", description="nothing yet", created_at=_ts(2026, 3, 3)))
        s.add_all([
            # created_at with a zero fraction: isoformat() drops it.
            Conversation(id="c-old", title="old", model="m", created_at=_ts(2026, 1, 1, 10)),
            Conversation(id="c-pinned", title=None, model="m", pinned=True, created_at=_ts(2026, 1, 2, 10, 0, 0, 5)),
            Conversation(id="c-ghost", title="incognito", model="m", ephemeral=True, created_at=_ts(2026, 1, 3)),
            Conversation(id="telegram_1", title="tg", model="m", surface="telegram", created_at=_ts(2026, 1, 4)),
            Conversation(id="c-proj", title="in project", model="m", project_id="p1", created_at=_ts(2026, 1, 5, 1, 2, 3, 456789)),
        ])
        tie = _ts(2026, 1, 1, 10, 5, 0, 123456)
        s.add_all([
            Message(id="m1", conversation_id="c-old", role="user", content="hi", created_at=_ts(2026, 1, 1, 10, 1)),
            Message(
                id="m2", conversation_id="c-old", role="assistant", content="hello", model="m",
                input_tokens=120, output_tokens=7, ttft_ms=350.5, llm_ms=900.0, prefill_tps=None,
                eval_tps=38.25, duration_ms=1500.0, created_at=_ts(2026, 1, 1, 10, 2),
            ),
            # Two messages on one timestamp: the cursor's id tiebreak decides.
            Message(id="m3a", conversation_id="c-old", role="user", content="tie a", created_at=tie),
            Message(id="m3b", conversation_id="c-old", role="user", content="tie b", created_at=tie),
            Message(id="m4", conversation_id="c-old", role="assistant", content="", status="error", created_at=_ts(2026, 1, 1, 10, 6)),
            Message(id="m5", conversation_id="c-proj", role="user", content="q", created_at=_ts(2026, 1, 5, 2)),
        ])
        # Inserted out of seq order: the Message.steps list is sorted by seq.
        s.add_all([
            Step(id="s3", message_id="m2", conversation_id="c-old", node="tools", source="main", data='{"x": 3}', seq=3, created_at=_ts(2026, 1, 1, 10, 1, 30)),
            Step(id="s1", message_id="m2", conversation_id="c-old", node="model", source="main", seq=1, created_at=_ts(2026, 1, 1, 10, 1, 10)),
            Step(id="s2", message_id="m2", conversation_id="c-old", node="tools", source="subagent", subagent="researcher:0", data=None, seq=2, created_at=_ts(2026, 1, 1, 10, 1, 20)),
        ])
        await s.commit()
    return {"conversation": "c-old", "project": "p1"}


# ── helpers ──────────────────────────────────────────────────────────────────


def _relay_text(operation: str) -> str:
    src = (GENERATED / f"{operation}.graphql.ts").read_text()
    match = re.search(r'"text": (".*?(?<!\\)")', src, re.S)
    assert match, f"no query text in {operation}"
    return json.loads(match.group(1))


async def _python(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    from db import async_session
    from server.graphql.extensions import SESSION_LOCK_KEY
    from server.graphql.schema import schema

    async with async_session() as s:
        res = await schema.execute(
            query, variable_values=variables, context_value={"session": s, SESSION_LOCK_KEY: asyncio.Lock()},
        )
    assert not res.errors, res.errors
    return {"data": res.data}


async def _edge(client: httpx.AsyncClient, query: str, variables: dict[str, Any] | None = None) -> httpx.Response:
    return await client.post("/graphql", json={"query": query, "variables": variables or {}})


async def _assert_same(client: httpx.AsyncClient, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    expected = await _python(query, variables)
    resp = await _edge(client, query, variables)
    assert resp.status_code == 200, f"edge proxied instead of answering ({resp.status_code})"
    assert resp.json() == expected
    return expected


# ── parity ───────────────────────────────────────────────────────────────────


async def test_conversation_list(seeded, edge):
    data = await _assert_same(edge, _relay_text("ConversationListQuery"))
    ids = [c["id"] for c in data["data"]["conversations"]]
    # Pinned first, ephemeral and non-web conversations hidden.
    assert ids[0] == _gid("Conversation", "c-pinned")
    assert _gid("Conversation", "c-ghost") not in ids
    assert _gid("Conversation", "telegram_1") not in ids


@pytest.mark.parametrize("surface", ["null", '"telegram"', '"web"', '"nowhere"'])
async def test_conversation_list_surface_argument(seeded, edge, surface):
    await _assert_same(edge, f"{{ conversations(surface: {surface}) {{ id surface ephemeral model projectId }} }}")


async def test_conversation_page_paginates_identically(seeded, edge):
    query = _relay_text("ConversationPageQuery")
    variables = {"id": _gid("Conversation", seeded["conversation"]), "count": 2, "cursor": None}
    pages = 0
    while True:
        data = await _assert_same(edge, query, variables)
        pages += 1
        info = data["data"]["conversation"]["messages"]["pageInfo"]
        if not info["hasPreviousPage"]:
            break
        variables["cursor"] = info["startCursor"]
    assert pages == 3  # 5 messages, 2 per page — the tie straddles a boundary


async def test_conversation_page_refetch_via_node(seeded, edge):
    await _assert_same(
        edge,
        _relay_text("ConversationPageRefetchQuery"),
        {"id": _gid("Conversation", seeded["conversation"]), "count": 10},
    )


async def test_conversation_with_project(seeded, edge):
    await _assert_same(
        edge,
        _relay_text("ConversationPageQuery"),
        {"id": _gid("Conversation", "c-proj"), "count": 10, "cursor": None},
    )


async def test_missing_conversation_is_null(seeded, edge):
    await _assert_same(edge, "query($id: ID!) { conversation(id: $id) { id } }", {"id": _gid("Conversation", "nope")})


async def test_projects(seeded, edge):
    await _assert_same(edge, _relay_text("ProjectsQuery"))
    await _assert_same(edge, _relay_text("ProjectQuery"), {"id": _gid("Project", seeded["project"])})
    await _assert_same(edge, _relay_text("ProjectQuery"), {"id": _gid("Project", "p2")})


async def test_node_resolves_messages_and_projects(seeded, edge):
    query = """query($id: ID!) { node(id: $id) { __typename id
        ... on Message { role content steps { id seq subagent data createdAt } }
        ... on Project { name conversationCount } } }"""
    await _assert_same(edge, query, {"id": _gid("Message", "m2")})
    await _assert_same(edge, query, {"id": _gid("Project", "p1")})


# ── the other domains ────────────────────────────────────────────────────────


@pytest.fixture
async def domains(database, work_dir: Path) -> dict[str, str]:
    """One of everything the remaining list/detail pages show, including the
    shapes that are easy to get subtly wrong."""
    from datetime import timedelta

    from db import async_session
    from db.models import (
        Approval, Artifact, ArtifactVersion, Automation, AutomationRun, BoardTask, BoardTaskLink,
        Conversation, Document, Memory, MemoryActivity, NotificationChannel, Skill, Workflow, WorkflowRun,
    )

    art_dir = work_dir / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "a-crlf.md").write_bytes(b"# Title\r\nline two\rline three\n")
    (art_dir / "a-crlf_v1.md").write_bytes(b"# Title\r\nv1")
    (art_dir / "a-crlf_v2.md").write_text("# Title\nv2")
    (art_dir / "a-binary.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    now = datetime.now(timezone.utc)

    async with async_session() as s:
        s.add(Conversation(id="c1", title="t", model="m", created_at=_ts(2026, 2, 1)))
        s.add_all([
            Artifact(id="a-crlf", title="Notes", filename=str(art_dir / "a-crlf.md"), conversation_id="c1",
                     message_id="m-x", created_at=_ts(2026, 2, 1, 1), updated_at=_ts(2026, 2, 1, 3)),
            Artifact(id="a-binary", title="Chart", filename=str(art_dir / "a-binary.png"), kind="image",
                     mime_type="image/png", conversation_id="c1", created_at=_ts(2026, 2, 1, 2), updated_at=_ts(2026, 2, 1, 2)),
            # The file is gone: content reads as "".
            Artifact(id="a-missing", title="Gone", filename=str(art_dir / "nope.md"), created_at=_ts(2026, 2, 2), updated_at=_ts(2026, 2, 2)),
            ArtifactVersion(id="v2", artifact_id="a-crlf", version=2, title="Notes", filename=str(art_dir / "a-crlf_v2.md"), created_at=_ts(2026, 2, 1, 3)),
            ArtifactVersion(id="v1", artifact_id="a-crlf", version=1, title="Notes", filename=str(art_dir / "a-crlf_v1.md"), created_at=_ts(2026, 2, 1, 1)),
            Document(id="d2", conversation_id="c1", filename="b.csv", mime_type="text/csv", size=10, path="/x/b", created_at=_ts(2026, 2, 1, 5)),
            Document(id="d1", conversation_id="c1", message_id="m-x", filename="a.pdf", mime_type="application/pdf", size=2048, path="/x/a", created_at=_ts(2026, 2, 1, 4)),
        ])
        s.add_all([
            # Day-of-month AND day-of-week, the APScheduler reading.
            Automation(id="au-and", name="first monday", input_type="prompt", prompt_text="p", schedule="0 9 1 * 1",
                       created_at=_ts(2026, 1, 1), updated_at=_ts(2026, 1, 2)),
            Automation(id="au-weekdays", name="weekdays", input_type="monitor", prompt_text="watch", schedule="30 8 * * 1-5",
                       notifications='[{"channel": "x"}]', created_at=_ts(2026, 1, 2), updated_at=_ts(2026, 1, 2)),
            Automation(id="au-off", name="disabled", input_type="code", code_text="print(1)", schedule="0 9 * * *",
                       enabled=False, stateful=True, created_at=_ts(2026, 1, 3), updated_at=_ts(2026, 1, 3)),
            Automation(id="au-hook", name="hook", input_type="webhook", webhook_url="http://x", webhook_method="POST",
                       webhook_headers='{"a": "b"}', webhook_body="{}", schedule="not a cron",
                       created_at=_ts(2026, 1, 4), updated_at=_ts(2026, 1, 4)),
            AutomationRun(id="r-old", automation_id="au-and", status="done", triggered_by="schedule", output="o",
                          started_at=now - timedelta(days=9), finished_at=now - timedelta(days=9)),
            AutomationRun(id="r-ok", automation_id="au-and", status="no_change", triggered_by="schedule",
                          started_at=now - timedelta(days=3), finished_at=now - timedelta(days=3)),
            AutomationRun(id="r-err", automation_id="au-and", status="error", triggered_by="manual", error="boom",
                          started_at=now - timedelta(days=1)),
            AutomationRun(id="r-w", automation_id="au-weekdays", status="running", triggered_by="manual",
                          started_at=now - timedelta(hours=1)),
        ])
        s.add_all([
            BoardTask(id="b-root", title="ship", priority=5, status="todo", created_at=_ts(2026, 3, 1)),
            BoardTask(id="b-a", title="part a", body="do a", priority=5, status="done", summary="did a",
                      result_metadata='{"k": 1}', job_id="job-1", started_at=_ts(2026, 3, 1, 1), finished_at=_ts(2026, 3, 1, 2),
                      created_at=_ts(2026, 3, 1, 0, 0, 1)),
            BoardTask(id="b-b", title="part b", priority=9, status="blocked", blocked_reason="?", blocked_kind="needs_input",
                      failure_count=2, created_by="agent", model="m", skill="s", created_at=_ts(2026, 3, 2)),
            BoardTask(id="b-arch", title="old", status="archived", created_at=_ts(2026, 2, 1)),
            BoardTaskLink(id="l1", parent_id="b-a", child_id="b-root"),
            BoardTaskLink(id="l2", parent_id="b-b", child_id="b-root"),
        ])
        s.add_all([
            Workflow(id="w1", name="flow", definition='{"nodes": [], "edges": []}', created_at=_ts(2026, 4, 1), updated_at=_ts(2026, 4, 2)),
            Workflow(id="w2", name="flow 2", description="d", notifications="[]", created_at=_ts(2026, 4, 3), updated_at=_ts(2026, 4, 3)),
            WorkflowRun(id="wr1", workflow_id="w1", status="done", inputs='{"a": 1}', outputs="{}", node_results="[]",
                        started_at=_ts(2026, 4, 2, 1), finished_at=_ts(2026, 4, 2, 2)),
            WorkflowRun(id="wr2", workflow_id="w1", status="error", error="x", started_at=_ts(2026, 4, 2, 3)),
        ])
        s.add_all([
            NotificationChannel(id="n2", name="discord", type="discord", target="123", created_at=_ts(2026, 5, 2)),
            NotificationChannel(id="n1", name="tg", type="telegram", target="-100", created_at=_ts(2026, 5, 1)),
            Skill(id="s2", name="zeta", description="z", body="Z", enabled=False, created_at=_ts(2026, 5, 1)),
            Skill(id="s1", name="alpha", description="a", body="A", created_at=_ts(2026, 5, 1)),
        ])
        s.add_all([
            Approval(id="ap-block", source="chat", kind="approval", question="delete?", label="Delete", tool="rm",
                     args_json='{"p": 1}', parent_id="c1", requested_at=_ts(2026, 6, 1, 1, 0, 0, 5)),
            Approval(id="ap-deferred", source="deferred", action="delete_workflow", requested_at=_ts(2026, 6, 1, 2)),
            Approval(id="ap-done", source="chat", status="approved", requested_at=_ts(2026, 6, 1, 3)),
        ])
        s.add_all([
            Memory(id="mem-core", kind="core", text="name is Sam", updated_at=_ts(2026, 7, 1)),
            Memory(id="mem-fact", kind="fact", text="likes tea", updated_at=_ts(2026, 7, 2, 0, 0, 0, 120)),
            MemoryActivity(id="ma1", memory_id="mem-fact", kind="fact", score=0.81, query="drinks", source="retrieval",
                           conversation_id="c1", accessed_at=_ts(2026, 7, 3)),
            MemoryActivity(id="ma2", memory_id="mem-fact", kind="fact", source="explicit_search", accessed_at=_ts(2026, 7, 4, 1, 2, 3, 4)),
        ])
        await s.commit()
    return {}


async def test_artifacts_and_documents(domains, edge):
    await _assert_same(edge, _relay_text("ArtifactListQuery"), {"conversationId": None})
    await _assert_same(edge, _relay_text("ArtifactListQuery"), {"conversationId": "c1"})
    for raw in ("a-crlf", "a-binary", "a-missing", "nope"):
        await _assert_same(edge, _relay_text("ArtifactDetailQuery"), {"id": _gid("Artifact", raw)})
    await _assert_same(edge, _relay_text("DocumentListQuery"), {"conversationId": "c1"})
    await _assert_same(edge, """query { artifactVersions(artifactId: "a-crlf") { id artifactId version title filename createdAt content } }""")
    await _assert_same(edge, """query { artifacts { id versionCount content versions { version content } } }""")


async def test_automation_runs(domains, edge):
    for automation in ("au-and", "au-weekdays", "au-hook"):
        await _assert_same(edge, _relay_text("AutomationRunsQuery"), {"automationId": _gid("Automation", automation)})


async def test_board(domains, edge):
    for include in (False, True):
        await _assert_same(edge, _relay_text("BoardTasksQuery"), {"includeArchived": include})
    fields = "id title parentIds childIds conversationId runId startedAt finishedAt"
    await _assert_same(edge, f'query($id: ID!) {{ boardTask(id: $id) {{ {fields} }} }}', {"id": _gid("BoardTask", "b-root")})
    # Through `node`, Python leaves the link lists empty; so does the edge.
    await _assert_same(edge, f'query($id: ID!) {{ node(id: $id) {{ ... on BoardTask {{ {fields} }} }} }}', {"id": _gid("BoardTask", "b-root")})


async def test_workflows(domains, edge):
    await _assert_same(edge, _relay_text("WorkflowListQuery"))
    await _assert_same(edge, _relay_text("WorkflowDetailQuery"), {"id": _gid("Workflow", "w1")})
    await _assert_same(edge, _relay_text("WorkflowRunsQuery"), {"workflowId": _gid("Workflow", "w1")})
    await _assert_same(edge, _relay_text("WorkflowRunDetailQuery"), {"id": _gid("WorkflowRun", "wr2")})


async def test_small_lists(domains, edge):
    await _assert_same(edge, _relay_text("NotificationChannelsQuery"))
    await _assert_same(edge, _relay_text("SkillsQuery"))
    data = await _assert_same(edge, _relay_text("PendingApprovalsQuery"))
    assert [a["id"] for a in data["data"]["pendingApprovals"]] == ["ap-deferred", "ap-block"]


async def test_memories(domains, edge):
    await _assert_same(edge, _relay_text("MemoriesQuery"))
    full = "id kind text updatedAt lastUsedAt useCount activities(limit: 1) { id memoryId conversationId kind score query source accessedAt }"
    await _assert_same(edge, f'{{ memories(kind: "fact") {{ {full} }} }}')
    await _assert_same(edge, f"{{ memoryUsage {{ {full} }} }}")
    await _assert_same(edge, '{ memoryActivities(memoryId: "mem-fact") { id accessedAt score } }')


@pytest.mark.parametrize("type_name, raw", [
    ("Artifact", "a-crlf"), ("Document", "d1"), ("AutomationRun", "r-err"),
    ("Workflow", "w2"), ("WorkflowRun", "wr1"), ("NotificationChannel", "n1"), ("Skill", "s2"),
])
async def test_node_resolves_every_type(domains, edge, type_name, raw):
    await _assert_same(edge, "query($id: ID!) { node(id: $id) { __typename id } }", {"id": _gid(type_name, raw)})


# ── mutations ────────────────────────────────────────────────────────────────
#
# A mutation's result can't be compared against a server that already ran it,
# so each one runs twice: through Python on the test database, and through the
# edge on a byte-identical copy (`twin`). Then the responses, every table and
# every artifact file are compared. Only what is generated fresh — uuid ids and
# "now" timestamps — is masked.

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(\.\d+)?(\+00:00)?$")


def _mask(value: Any, since: datetime, dirs: tuple[str, ...]) -> Any:
    """Replace what legitimately differs between the two runs."""
    import base64

    if isinstance(value, dict):
        return {k: _mask(v, since, dirs) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, since, dirs) for v in value]
    if not isinstance(value, str):
        return value
    for d in dirs:
        value = value.replace(d, "<dir>")
    if m := _STAMP.match(value):
        stamp = datetime.fromisoformat(f"{m[1]}T{m[2]}{m[3] or ''}").replace(tzinfo=timezone.utc)
        if stamp >= since:
            return "<now>"
    with contextlib.suppress(Exception):
        decoded = base64.b64decode(value, validate=True).decode()
        if ":" in decoded and _UUID.search(decoded):
            return "<new-gid>"
    return _UUID.sub("<uuid>", value)


def _dump(db: Path) -> dict[str, list[dict]]:
    """Every row of every app table, order-independent."""
    import sqlite3

    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'"
        )]
        return {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}


def _files(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.glob("*"))} if directory.exists() else {}


class Twin:
    """Python on the test database, the edge on a copy of it."""

    def __init__(self, edge: httpx.AsyncClient, a_dir: Path, b_dir: Path):
        self.edge, self.a_dir, self.b_dir = edge, a_dir, b_dir
        self.dirs = (str(a_dir), str(b_dir))
        # Anything stamped after the copy was taken was written by a mutation
        # under test, on both sides, milliseconds apart.
        self.since = datetime.now(timezone.utc).replace(microsecond=0)

    async def run(
        self, query: str, variables: dict[str, Any] | None = None, *, edge_variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run on both sides and diff. `edge_variables` is for ids each side
        minted for itself (a run's, a queued message's)."""
        from db import async_session
        from server.graphql.extensions import SESSION_LOCK_KEY
        from server.graphql.schema import schema

        since = self.since
        async with async_session() as s:
            res = await schema.execute(
                query, variable_values=variables,
                context_value={"session": s, SESSION_LOCK_KEY: asyncio.Lock(), "caller": "human"},
            )
        python = {"data": res.data}
        if res.errors:
            python["errors"] = [{"message": e.message, "path": e.path} for e in res.errors]

        edge_vars = variables if edge_variables is None else edge_variables
        resp = await self.edge.post("/graphql", json={"query": query, "variables": edge_vars or {}})
        assert resp.status_code == 200, f"edge proxied instead of answering ({resp.status_code})"
        body = resp.json()
        edge = {"data": body.get("data")}
        if body.get("errors"):
            edge["errors"] = [{"message": e["message"], "path": e.get("path")} for e in body["errors"]]

        assert _mask(edge, since, self.dirs) == _mask(python, since, self.dirs), query
        a, b = _dump(self.a_dir / "database.db"), _dump(self.b_dir / "database.db")
        for table in a:
            assert _mask(b[table], since, self.dirs) == _mask(a[table], since, self.dirs), f"{table} after {query}"
        assert _files(self.b_dir / "artifacts") == _files(self.a_dir / "artifacts"), f"files after {query}"
        return python


@pytest.fixture
async def twin(seeded, domains, work_dir: Path, tmp_path_factory, edge_binary: Path):
    import sqlite3

    from db import async_session
    from db.models import Artifact, DocumentChunk

    async with async_session() as s:
        # One markdown artifact with a live file and no history (the v1
        # migration path), and indexed chunks for a document.
        (work_dir / "artifacts" / "a-plain.md").write_bytes(b"old\r\nbody")
        s.add(Artifact(id="a-plain", title="Plain", filename=str(work_dir / "artifacts" / "a-plain.md"),
                       created_at=_ts(2026, 2, 3), updated_at=_ts(2026, 2, 3)))
        s.add_all([
            DocumentChunk(id="ch1", document_id="d1", conversation_id="c1", seq=0, text="alpha beta"),
            DocumentChunk(id="ch2", document_id="d1", conversation_id="c1", seq=1, text="gamma"),
        ])
        await s.commit()

    b_dir = tmp_path_factory.mktemp("twin")
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as src, \
            contextlib.closing(sqlite3.connect(b_dir / "database.db")) as dst:
        src.backup(dst)
        # The copy's file paths point at its own artifact directory.
        for table, col in (("artifacts", "filename"), ("artifact_versions", "filename")):
            dst.execute(f"UPDATE {table} SET {col} = replace({col}, ?, ?)", (str(work_dir), str(b_dir)))
        dst.commit()
    shutil.copytree(work_dir / "artifacts", b_dir / "artifacts")

    async with _run_edge(edge_binary, b_dir, b_dir / "database.db") as client:
        yield Twin(client, work_dir, b_dir)


async def test_project_mutations(twin):
    fields = "id name description instructions memory createdAt updatedAt conversationCount"
    create = f"mutation($input: ProjectCreateInput!) {{ createProject(input: $input) {{ {fields} }} }}"
    await twin.run(create, {"input": {"name": "  Spaced  ", "description": None}})
    await twin.run(create, {"input": {"name": "Full", "description": "d", "instructions": "be brief"}})
    await twin.run(create, {"input": {"name": "   "}})

    update = f"mutation($id: ID!, $input: ProjectUpdateInput!) {{ updateProject(id: $id, input: $input) {{ {fields} }} }}"
    p1 = _gid("Project", "p1")
    await twin.run(update, {"id": p1, "input": {"name": " Renamed ", "memory": "- new fact"}})
    await twin.run(update, {"id": p1, "input": {"description": "", "instructions": "x"}})
    await twin.run(update, {"id": p1, "input": {}})
    await twin.run(update, {"id": p1, "input": {"name": " "}})
    await twin.run(update, {"id": _gid("Project", "nope"), "input": {"name": "x"}})

    member = "mutation($c: ID!, $p: ID) { setConversationProject(conversationId: $c, projectId: $p) { id projectId project { id } } }"
    await twin.run(member, {"c": _gid("Conversation", "c-old"), "p": _gid("Project", "p2")})
    await twin.run(member, {"c": _gid("Conversation", "c-old"), "p": None})
    await twin.run(member, {"c": _gid("Conversation", "telegram_1"), "p": p1})
    await twin.run(member, {"c": _gid("Conversation", "c-old"), "p": _gid("Project", "nope")})
    await twin.run(member, {"c": _gid("Conversation", "nope"), "p": p1})

    delete = "mutation($id: ID!) { deleteProject(id: $id) }"
    await twin.run(delete, {"id": p1})  # c-proj keeps existing, unlinked
    await twin.run(delete, {"id": p1})


async def test_notification_channel_mutations(twin):
    fields = "id name type target createdAt updatedAt"
    create = f"mutation($input: NotificationChannelCreateInput!) {{ createNotificationChannel(input: $input) {{ {fields} }} }}"
    await twin.run(create, {"input": {"name": " ops ", "type": "telegram", "target": " -42 "}})
    await twin.run(create, {"input": {"name": "x", "type": "discord", "target": "  "}})
    await twin.run(create, {"input": {"name": "", "type": "discord", "target": "1"}})

    update = f"mutation($id: ID!, $input: NotificationChannelUpdateInput!) {{ updateNotificationChannel(id: $id, input: $input) {{ {fields} }} }}"
    n1 = _gid("NotificationChannel", "n1")
    await twin.run(update, {"id": n1, "input": {"name": " renamed ", "target": " 7 "}})
    await twin.run(update, {"id": n1, "input": {}})  # still bumps updatedAt
    await twin.run(update, {"id": n1, "input": {"target": " "}})
    await twin.run(update, {"id": _gid("NotificationChannel", "nope"), "input": {"name": "x"}})

    # A workflow that delivers to n1 blocks deleting it.
    await twin.run(
        "mutation($id: ID!, $input: WorkflowUpdateInput!) { updateWorkflow(id: $id, input: $input) { id } }",
        {"id": _gid("Workflow", "w2"), "input": {"notifications": '[{"id": "n1"}, "junk"]'}},
    )
    delete = "mutation($id: ID!) { deleteNotificationChannel(id: $id) }"
    await twin.run(delete, {"id": n1})
    await twin.run(delete, {"id": _gid("NotificationChannel", "n2")})
    await twin.run(delete, {"id": _gid("NotificationChannel", "n2")})


async def test_workflow_mutations(twin):
    fields = "id name description definition notifications createdAt updatedAt"
    create = f"mutation($input: WorkflowCreateInput!) {{ createWorkflow(input: $input) {{ {fields} }} }}"
    await twin.run(create, {"input": {"name": "new"}})  # definition defaults to "{}"
    await twin.run(create, {"input": {"name": "  untrimmed ", "description": "d", "definition": '{"nodes": [1]}', "notifications": "[]"}})

    update = f"mutation($id: ID!, $input: WorkflowUpdateInput!) {{ updateWorkflow(id: $id, input: $input) {{ {fields} }} }}"
    w1 = _gid("Workflow", "w1")
    await twin.run(update, {"id": w1, "input": {}})  # unchanged, no bump
    await twin.run(update, {"id": w1, "input": {"name": "renamed", "definition": "{}"}})
    await twin.run(update, {"id": _gid("Workflow", "nope"), "input": {"name": "x"}})
    await twin.run(update, {"id": _gid("Workflow", "nope"), "input": {}})

    delete = "mutation($id: ID!) { deleteWorkflow(id: $id) }"
    await twin.run(delete, {"id": w1})  # its runs go too
    await twin.run(delete, {"id": w1})


async def test_skill_memory_and_conversation_mutations(twin):
    await twin.run("mutation($id: ID!) { deleteSkill(id: $id) }", {"id": _gid("Skill", "s1")})
    await twin.run("mutation($id: ID!) { deleteSkill(id: $id) }", {"id": _gid("Skill", "s1")})
    # The access log outlives the item, as it does in Python.
    await twin.run('mutation { deleteMemory(id: "mem-fact") }')
    await twin.run('mutation { deleteMemory(id: "mem-fact") }')

    update = "mutation($id: ID!, $title: String, $pinned: Boolean) { updateConversation(id: $id, title: $title, pinned: $pinned) { id title pinned model } }"
    c = _gid("Conversation", "c-old")
    await twin.run(update, {"id": c, "title": "Renamed", "pinned": None})
    await twin.run(update, {"id": c, "pinned": True})
    await twin.run(update, {"id": c, "title": "", "pinned": False})
    await twin.run(update, {"id": c})
    await twin.run(update, {"id": _gid("Conversation", "nope"), "pinned": True})


async def test_artifact_and_document_mutations(twin):
    fields = "id title filename kind updatedAt content versionCount versions { version title filename content createdAt }"
    update = f"mutation($id: ID!, $title: String, $content: String) {{ updateArtifact(id: $id, title: $title, content: $content) {{ {fields} }} }}"
    await twin.run(update, {"id": _gid("Artifact", "a-crlf"), "title": "Retitled"})
    await twin.run(update, {"id": _gid("Artifact", "a-crlf"), "content": "# v3\nbody"})  # v1, v2 exist → v3
    await twin.run(update, {"id": _gid("Artifact", "a-plain"), "title": "Both", "content": "new"})  # migrates v1
    await twin.run(update, {"id": _gid("Artifact", "a-missing"), "content": "first"})  # no live file → v1 is new
    await twin.run(update, {"id": _gid("Artifact", "a-binary"), "content": "x"})
    await twin.run(update, {"id": _gid("Artifact", "nope"), "title": "x"})

    restore = f"mutation($id: ID!, $v: Int!) {{ restoreArtifactVersion(id: $id, version: $v) {{ {fields} }} }}"
    await twin.run(restore, {"id": _gid("Artifact", "a-crlf"), "v": 1})
    await twin.run(restore, {"id": _gid("Artifact", "a-crlf"), "v": 99})

    await twin.run("mutation($id: ID!) { deleteArtifact(id: $id) }", {"id": _gid("Artifact", "a-crlf")})
    await twin.run("mutation($id: ID!) { deleteArtifact(id: $id) }", {"id": _gid("Artifact", "a-crlf")})
    # Chunks go with the document; the FTS delete triggers must run in the edge's SQLite too.
    await twin.run("mutation($id: ID!) { deleteDocument(id: $id) }", {"id": _gid("Document", "d1")})
    await twin.run("mutation($id: ID!) { deleteDocument(id: $id) }", {"id": _gid("Document", "d1")})


async def test_conditionally_owned_mutations_are_proxied(seeded, edge):
    # A model change needs the catalog, which is in Python.
    resp = await edge.post("/graphql", json={
        "query": "mutation($id: ID!, $m: String) { updateConversation(id: $id, model: $m) { id } }",
        "variables": {"id": _gid("Conversation", "c-old"), "m": "google_genai:x"},
    })
    assert resp.status_code == 502
    # An agent's delete is approval-gated in Python; a human's isn't.
    for mutation in ("deleteWorkflow", "deleteSkill"):
        q = f'mutation {{ {mutation}(id: "{_gid("Workflow", "nope")}") }}'
        assert (await edge.post("/graphql", json={"query": q}, headers={"X-Jarvis-Caller": "agent"})).status_code == 502
        assert (await edge.post("/graphql", json={"query": q})).status_code == 200


# ── routing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        # A root field the edge doesn't implement.
        '{ todos(conversationId: "c-old") { text } }',
        # One owned root field and one not: the whole operation goes to Python.
        '{ conversations { id } todos(conversationId: "c-old") { text } }',
        # Owned root field, un-ported subfield: validation fails, so it's proxied.
        "{ conversations { id notAField } }",
        # Automation stays with the scheduler: nextRunAt is APScheduler's answer.
        "{ automations { id nextRunAt } }",
        '{ node(id: "QXV0b21hdGlvbjphdS1hbmQ=") { id } }',
        # Mutations always go to Python in this phase.
        'mutation { deleteConversation(id: "x") }',
        # A node id of a type the edge can't resolve.
        '{ node(id: "UnVubmluZ1Rhc2s6YWJj") { id } }',
    ],
)
async def test_unowned_operations_are_proxied(seeded, edge, query):
    resp = await _edge(edge, query)
    assert resp.status_code == 502  # the dead backend — i.e. it was proxied


# ── schema contract ──────────────────────────────────────────────────────────


def _signature(field) -> tuple[str, dict[str, tuple[str, Any]]]:
    from graphql import Undefined

    args = {
        name: (str(arg.type), None if arg.default_value is Undefined else arg.default_value)
        for name, arg in field.args.items()
    }
    return str(field.type), args


def _input_signature(field) -> tuple[str, Any]:
    from graphql import Undefined

    return str(field.type), None if field.default_value is Undefined else field.default_value


def test_edge_schema_is_a_subset_of_python(edge_binary):
    """Every type and field the edge defines exists in Python with the same
    type and arguments — the edge may lag Python, never contradict it."""
    from graphql import GraphQLInputObjectType, GraphQLInterfaceType, GraphQLObjectType, build_schema

    from server.graphql.schema import schema

    rust = build_schema(subprocess.run([str(edge_binary), "--print-schema"], capture_output=True, text=True, check=True).stdout)
    python = build_schema(schema.as_str())

    for name, rtype in rust.type_map.items():
        if name.startswith("__"):
            continue
        if isinstance(rtype, GraphQLInputObjectType):
            ptype = python.type_map.get(name)
            assert isinstance(ptype, GraphQLInputObjectType), f"input {name} is not in the Python schema"
            assert {f: _input_signature(v) for f, v in rtype.fields.items()} == {
                f: _input_signature(v) for f, v in ptype.fields.items()
            }, f"input {name} differs"
            continue
        if not isinstance(rtype, (GraphQLObjectType, GraphQLInterfaceType)):
            continue
        ptype = python.type_map.get(name)
        assert ptype is not None, f"{name} is not in the Python schema"
        for fname, rfield in rtype.fields.items():
            assert fname in ptype.fields, f"{name}.{fname} is not in the Python schema"
            assert _signature(rfield) == _signature(ptype.fields[fname]), f"{name}.{fname} differs"
        # A non-root type is fully ported or not at all: a missing field would
        # make an owned operation fail validation and fall back on every call.
        if name not in ("Query", "Mutation"):
            assert set(rtype.fields) == set(ptype.fields), f"{name} is partially ported"


def test_every_claimed_frontend_query_is_diffed(edge_binary):
    from graphql import OperationDefinitionNode, build_schema, parse

    rust = build_schema(subprocess.run([str(edge_binary), "--print-schema"], capture_output=True, text=True, check=True).stdout)
    owned = set(rust.query_type.fields) - {"node"}

    claimed = set()
    for path in GENERATED.glob("*Query.graphql.ts"):
        op = path.name.removesuffix(".graphql.ts")
        doc = parse(_relay_text(op))
        for definition in doc.definitions:
            if isinstance(definition, OperationDefinitionNode) and definition.operation.value == "query":
                roots = {sel.name.value for sel in definition.selection_set.selections if hasattr(sel, "name")}
                if roots and roots <= owned:
                    claimed.add(op)
    assert claimed <= PARITY_OPERATIONS, f"add a parity test for {sorted(claimed - PARITY_OPERATIONS)}"
