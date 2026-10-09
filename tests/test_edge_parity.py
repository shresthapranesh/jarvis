"""The Rust server must answer exactly what the Python schema answered.

The two servers had to be indistinguishable to the frontend: same JSON, same
global ids, same timestamp strings, same cursors. These tests seed one
database, ask the frontend's *real* Relay operations (read from
`frontend/src/__generated__`), and diff the answers — and, after a mutation,
every row and artifact file — against Python's, recorded while it existed
(`python_golden.py`).

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from edge_support import GENERATED, _gid, _relay_text, _run_edge, edge_binary, startup_sweep  # noqa: F401 — edge_binary is a fixture
from python_golden import recorded, started
from seed import insert

ROOT = Path(__file__).resolve().parent.parent

# Every frontend query whose root fields the edge implements must be listed
# here, so porting a field can't silently route an operation nobody diffed.
PARITY_OPERATIONS = {
    "AgentMemoryQuery",
    "ConversationListQuery",
    "ConversationPageQuery",
    "ConversationPageRefetchQuery",
    "ProjectsQuery",
    "ProjectQuery",
    "ArtifactDetailQuery",
    "ArtifactListQuery",
    "AutomationListQuery",
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
    "SettingsQuery",
    "ToolsQuery",
    # tests/test_edge_mcp.py
    "McpServersQuery",
    # Diffed against live runs in test_edge_runs.py.
    "RunningTasksQuery",
    # tests/test_edge_serving.py
    "ModelCatalogQuery",
    "useModelsQuery",
    "TodoListQuery",
    "BrowserAvailableQuery",
    # tests/test_edge_model_sync.py
    "ModelSyncQuery",
}


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def one_zone(monkeypatch):
    """The scheduler zone the recordings were made in (`Automation.nextRunAt`), with DST."""
    monkeypatch.setenv("JARVIS_TIMEZONE", "America/New_York")


@pytest.fixture
async def edge(database, work_dir: Path, edge_binary: Path, one_zone):
    """The edge on the test database. Tests ask for it before they seed, so
    its startup sweep finds nothing and the rows stay as seeded."""
    global _dirs
    _dirs = (str(work_dir),)
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db") as client:
        yield client


def _ts(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
async def seeded(database) -> dict[str, str]:
    """A small database exercising every shape the slice has to reproduce."""
    insert(database, "projects",
        id="p1", name="Thesis", description=None, instructions="cite sources",
        memory="- prefers APA", created_at=_ts(2026, 3, 1, 9), updated_at=_ts(2026, 3, 2, 9, 30, 0, 250),
    )
    insert(database, "projects", id="p2", name="Empty", description="nothing yet", created_at=_ts(2026, 3, 3))
    # created_at with a zero fraction: isoformat() drops it.
    insert(database, "conversations", id="c-old", title="old", model="m", created_at=_ts(2026, 1, 1, 10))
    insert(database, "conversations", id="c-pinned", title=None, model="m", pinned=True, created_at=_ts(2026, 1, 2, 10, 0, 0, 5))
    insert(database, "conversations", id="c-ghost", title="incognito", model="m", ephemeral=True, created_at=_ts(2026, 1, 3))
    insert(database, "conversations", id="telegram_1", title="tg", model="m", surface="telegram", created_at=_ts(2026, 1, 4))
    insert(database, "conversations", id="c-proj", title="in project", model="m", project_id="p1", created_at=_ts(2026, 1, 5, 1, 2, 3, 456789))

    tie = _ts(2026, 1, 1, 10, 5, 0, 123456)
    insert(database, "messages", id="m1", conversation_id="c-old", role="user", content="hi", created_at=_ts(2026, 1, 1, 10, 1))
    insert(database, "messages",
        id="m2", conversation_id="c-old", role="assistant", content="hello", model="m",
        input_tokens=120, output_tokens=7, ttft_ms=350.5, llm_ms=900.0, prefill_tps=None,
        eval_tps=38.25, duration_ms=1500.0, created_at=_ts(2026, 1, 1, 10, 2),
    )
    # Two messages on one timestamp: the cursor's id tiebreak decides.
    insert(database, "messages", id="m3a", conversation_id="c-old", role="user", content="tie a", created_at=tie)
    insert(database, "messages", id="m3b", conversation_id="c-old", role="user", content="tie b", created_at=tie)
    insert(database, "messages", id="m4", conversation_id="c-old", role="assistant", content="", status="error", created_at=_ts(2026, 1, 1, 10, 6))
    insert(database, "messages", id="m5", conversation_id="c-proj", role="user", content="q", created_at=_ts(2026, 1, 5, 2))

    # Inserted out of seq order: the Message.steps list is sorted by seq.
    insert(database, "steps", id="s3", message_id="m2", conversation_id="c-old", node="tools", source="main", data='{"x": 3}', seq=3, created_at=_ts(2026, 1, 1, 10, 1, 30))
    insert(database, "steps", id="s1", message_id="m2", conversation_id="c-old", node="model", source="main", seq=1, created_at=_ts(2026, 1, 1, 10, 1, 10))
    insert(database, "steps", id="s2", message_id="m2", conversation_id="c-old", node="tools", source="subagent", subagent="researcher:0", data=None, seq=2, created_at=_ts(2026, 1, 1, 10, 1, 20))

    return {"conversation": "c-old", "project": "p1"}


# ── helpers ──────────────────────────────────────────────────────────────────


async def _edge(client: httpx.AsyncClient, query: str, variables: dict[str, Any] | None = None) -> httpx.Response:
    return await client.post("/graphql", json={"query": query, "variables": variables or {}})


# The running test's work dir, which answers carry in artifact paths.
_dirs: tuple[str, ...] = ()


async def _assert_same(client: httpx.AsyncClient, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    expected = await recorded()
    resp = await _edge(client, query, variables)
    assert resp.status_code == 200
    assert _mask(resp.json(), started(), _dirs) == expected
    return expected


# ── parity ───────────────────────────────────────────────────────────────────


async def test_conversation_list(edge, seeded):
    data = await _assert_same(edge, _relay_text("ConversationListQuery"))
    ids = [c["id"] for c in data["data"]["conversations"]]
    # Pinned first, ephemeral and non-web conversations hidden.
    assert ids[0] == _gid("Conversation", "c-pinned")
    assert _gid("Conversation", "c-ghost") not in ids
    assert _gid("Conversation", "telegram_1") not in ids


@pytest.mark.parametrize("surface", ["null", '"telegram"', '"web"', '"nowhere"'])
async def test_conversation_list_surface_argument(edge, seeded, surface):
    await _assert_same(edge, f"{{ conversations(surface: {surface}) {{ id surface ephemeral model projectId }} }}")


async def test_conversation_page_paginates_identically(edge, seeded):
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


async def test_conversation_page_refetch_via_node(edge, seeded):
    await _assert_same(
        edge,
        _relay_text("ConversationPageRefetchQuery"),
        {"id": _gid("Conversation", seeded["conversation"]), "count": 10},
    )


async def test_conversation_with_project(edge, seeded):
    await _assert_same(
        edge,
        _relay_text("ConversationPageQuery"),
        {"id": _gid("Conversation", "c-proj"), "count": 10, "cursor": None},
    )


async def test_missing_conversation_is_null(edge, seeded):
    await _assert_same(edge, "query($id: ID!) { conversation(id: $id) { id } }", {"id": _gid("Conversation", "nope")})


async def test_projects(edge, seeded):
    await _assert_same(edge, _relay_text("ProjectsQuery"))
    await _assert_same(edge, _relay_text("ProjectQuery"), {"id": _gid("Project", seeded["project"])})
    await _assert_same(edge, _relay_text("ProjectQuery"), {"id": _gid("Project", "p2")})


async def test_node_resolves_messages_and_projects(edge, seeded):
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

    art_dir = work_dir / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "a-crlf.md").write_bytes(b"# Title\r\nline two\rline three\n")
    (art_dir / "a-crlf_v1.md").write_bytes(b"# Title\r\nv1")
    (art_dir / "a-crlf_v2.md").write_text("# Title\nv2")
    (art_dir / "a-binary.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    now = datetime.now(timezone.utc)

    insert(database, "conversations", id="c1", title="t", model="m", created_at=_ts(2026, 2, 1))
    insert(database, "artifacts", id="a-crlf", title="Notes", filename=str(art_dir / "a-crlf.md"), conversation_id="c1",
           message_id="m-x", created_at=_ts(2026, 2, 1, 1), updated_at=_ts(2026, 2, 1, 3))
    insert(database, "artifacts", id="a-binary", title="Chart", filename=str(art_dir / "a-binary.png"), kind="image",
           mime_type="image/png", conversation_id="c1", created_at=_ts(2026, 2, 1, 2), updated_at=_ts(2026, 2, 1, 2))
    # The file is gone: content reads as "".
    insert(database, "artifacts", id="a-missing", title="Gone", filename=str(art_dir / "nope.md"), created_at=_ts(2026, 2, 2), updated_at=_ts(2026, 2, 2))
    insert(database, "artifact_versions", id="v2", artifact_id="a-crlf", version=2, title="Notes", filename=str(art_dir / "a-crlf_v2.md"), created_at=_ts(2026, 2, 1, 3))
    insert(database, "artifact_versions", id="v1", artifact_id="a-crlf", version=1, title="Notes", filename=str(art_dir / "a-crlf_v1.md"), created_at=_ts(2026, 2, 1, 1))

    # Day-of-month AND day-of-week, the APScheduler reading.
    insert(database, "automations", id="au-and", name="first monday", input_type="prompt", prompt_text="p", schedule="0 9 1 * 1",
           created_at=_ts(2026, 1, 1), updated_at=_ts(2026, 1, 2))
    insert(database, "automations", id="au-weekdays", name="weekdays", input_type="monitor", prompt_text="watch", schedule="30 8 * * 1-5",
           notifications='[{"channel": "x"}]', created_at=_ts(2026, 1, 2), updated_at=_ts(2026, 1, 2))
    insert(database, "automations", id="au-off", name="disabled", input_type="code", code_text="print(1)", schedule="0 9 * * *",
           enabled=False, stateful=True, created_at=_ts(2026, 1, 3), updated_at=_ts(2026, 1, 3))
    insert(database, "automations", id="au-hook", name="hook", input_type="webhook", webhook_url="http://x", webhook_method="POST",
           webhook_headers='{"a": "b"}', webhook_body="{}", schedule="not a cron",
           created_at=_ts(2026, 1, 4), updated_at=_ts(2026, 1, 4))
    insert(database, "automation_runs", id="r-old", automation_id="au-and", status="done", triggered_by="schedule", output="o",
           started_at=now - timedelta(days=9), finished_at=now - timedelta(days=9))
    insert(database, "automation_runs", id="r-ok", automation_id="au-and", status="no_change", triggered_by="schedule",
           started_at=now - timedelta(days=3), finished_at=now - timedelta(days=3))
    insert(database, "automation_runs", id="r-err", automation_id="au-and", status="error", triggered_by="manual", error="boom",
           started_at=now - timedelta(days=1))
    insert(database, "automation_runs", id="r-w", automation_id="au-weekdays", status="running", triggered_by="manual",
           started_at=now - timedelta(hours=1))

    insert(database, "board_tasks", id="b-root", title="ship", priority=5, status="todo", created_at=_ts(2026, 3, 1))
    insert(database, "board_tasks", id="b-a", title="part a", body="do a", priority=5, status="done", summary="did a",
           result_metadata='{"k": 1}', job_id="job-1", started_at=_ts(2026, 3, 1, 1), finished_at=_ts(2026, 3, 1, 2),
           created_at=_ts(2026, 3, 1, 0, 0, 1))
    insert(database, "board_tasks", id="b-b", title="part b", priority=9, status="blocked", blocked_reason="?", blocked_kind="needs_input",
           failure_count=2, created_by="agent", model="m", skill="s", created_at=_ts(2026, 3, 2))
    insert(database, "board_tasks", id="b-arch", title="old", status="archived", created_at=_ts(2026, 2, 1))
    insert(database, "board_task_links", id="l1", parent_id="b-a", child_id="b-root")
    insert(database, "board_task_links", id="l2", parent_id="b-b", child_id="b-root")

    insert(database, "workflows", id="w1", name="flow", definition='{"nodes": [], "edges": []}', created_at=_ts(2026, 4, 1), updated_at=_ts(2026, 4, 2))
    insert(database, "workflows", id="w2", name="flow 2", description="d", notifications="[]", created_at=_ts(2026, 4, 3), updated_at=_ts(2026, 4, 3))
    insert(database, "workflow_runs", id="wr1", workflow_id="w1", status="done", inputs='{"a": 1}', outputs="{}", node_results="[]",
           started_at=_ts(2026, 4, 2, 1), finished_at=_ts(2026, 4, 2, 2))
    insert(database, "workflow_runs", id="wr2", workflow_id="w1", status="error", error="x", started_at=_ts(2026, 4, 2, 3))

    insert(database, "notification_channels", id="n2", name="discord", type="discord", target="123", created_at=_ts(2026, 5, 2))
    insert(database, "notification_channels", id="n1", name="tg", type="telegram", target="-100", created_at=_ts(2026, 5, 1))
    insert(database, "skills", id="s2", name="zeta", description="z", body="Z", enabled=False, created_at=_ts(2026, 5, 1))
    insert(database, "skills", id="s1", name="alpha", description="a", body="A", created_at=_ts(2026, 5, 1))

    insert(database, "approvals", id="ap-block", source="chat", kind="approval", question="delete?", label="Delete", tool="rm",
           args_json='{"p": 1}', parent_id="c1", requested_at=_ts(2026, 6, 1, 1, 0, 0, 5),
           updated_at=_ts(2026, 6, 1, 1, 0, 0, 5))
    insert(database, "approvals", id="ap-deferred", source="deferred", action="delete_workflow", requested_at=_ts(2026, 6, 1, 2),
           updated_at=_ts(2026, 6, 1, 2))
    insert(database, "approvals", id="ap-done", source="chat", status="approved", requested_at=_ts(2026, 6, 1, 3),
           updated_at=_ts(2026, 6, 1, 3))

    insert(database, "memories", id="mem-core", kind="core", text="name is Sam", updated_at=_ts(2026, 7, 1))
    insert(database, "memories", id="mem-fact", kind="fact", text="likes tea", updated_at=_ts(2026, 7, 2, 0, 0, 0, 120))
    insert(database, "memory_activities", id="ma1", memory_id="mem-fact", kind="fact", score=0.81, query="drinks", source="retrieval",
           conversation_id="c1", accessed_at=_ts(2026, 7, 3))
    insert(database, "memory_activities", id="ma2", memory_id="mem-fact", kind="fact", source="explicit_search", accessed_at=_ts(2026, 7, 4, 1, 2, 3, 4))

    return {}


async def test_artifacts(edge, domains):
    await _assert_same(edge, _relay_text("ArtifactListQuery"), {"conversationId": None})
    await _assert_same(edge, _relay_text("ArtifactListQuery"), {"conversationId": "c1"})
    for raw in ("a-crlf", "a-binary", "a-missing", "nope"):
        await _assert_same(edge, _relay_text("ArtifactDetailQuery"), {"id": _gid("Artifact", raw)})
    await _assert_same(edge, """query { artifactVersions(artifactId: "a-crlf") { id artifactId version title filename createdAt content } }""")
    await _assert_same(edge, """query { artifacts { id versionCount content versions { version content } } }""")


async def test_automation_runs(edge, domains):
    for automation in ("au-and", "au-weekdays", "au-hook"):
        await _assert_same(edge, _relay_text("AutomationRunsQuery"), {"automationId": _gid("Automation", automation)})


async def test_automations(edge, domains):
    """Including `nextRunAt`, which is the edge's scheduler's answer now:
    day-of-month AND day-of-week, Unix weekdays, a disabled schedule, and one
    that doesn't parse."""
    data = await _assert_same(edge, _relay_text("AutomationListQuery"))
    by_id = {a["name"]: a for a in data["data"]["automations"]}
    assert by_id["weekdays"]["nextRunAt"] and by_id["disabled"]["nextRunAt"] is None
    assert by_id["hook"]["nextRunAt"] is None
    fields = "id name inputType schedule enabled stateful conversationId nextRunAt lastRunStatus totalCount7d createdAt"
    for raw in ("au-and", "au-weekdays", "au-off", "au-hook", "missing"):
        await _assert_same(edge, f"query($id: ID!) {{ automation(id: $id) {{ {fields} }} }}", {"id": _gid("Automation", raw)})
    await _assert_same(edge, f"query($id: ID!) {{ node(id: $id) {{ ... on Automation {{ {fields} }} }} }}",
                       {"id": _gid("Automation", "au-weekdays")})


async def test_board(edge, domains):
    for include in (False, True):
        await _assert_same(edge, _relay_text("BoardTasksQuery"), {"includeArchived": include})
    fields = "id title parentIds childIds conversationId runId startedAt finishedAt"
    await _assert_same(edge, f'query($id: ID!) {{ boardTask(id: $id) {{ {fields} }} }}', {"id": _gid("BoardTask", "b-root")})
    # Through `node`, Python leaves the link lists empty; so does the edge.
    await _assert_same(edge, f'query($id: ID!) {{ node(id: $id) {{ ... on BoardTask {{ {fields} }} }} }}', {"id": _gid("BoardTask", "b-root")})


async def test_workflows(edge, domains):
    await _assert_same(edge, _relay_text("WorkflowListQuery"))
    await _assert_same(edge, _relay_text("WorkflowDetailQuery"), {"id": _gid("Workflow", "w1")})
    await _assert_same(edge, _relay_text("WorkflowRunsQuery"), {"workflowId": _gid("Workflow", "w1")})
    await _assert_same(edge, _relay_text("WorkflowRunDetailQuery"), {"id": _gid("WorkflowRun", "wr2")})


async def test_small_lists(edge, domains):
    await _assert_same(edge, _relay_text("NotificationChannelsQuery"))
    await _assert_same(edge, _relay_text("SkillsQuery"))
    data = await _assert_same(edge, _relay_text("PendingApprovalsQuery"))
    assert [a["id"] for a in data["data"]["pendingApprovals"]] == ["ap-deferred", "ap-block"]


async def test_memories(edge, domains):
    await _assert_same(edge, _relay_text("MemoriesQuery"))
    full = "id kind text updatedAt lastUsedAt useCount activities(limit: 1) { id memoryId conversationId kind score query source accessedAt }"
    await _assert_same(edge, f'{{ memories(kind: "fact") {{ {full} }} }}')
    await _assert_same(edge, f"{{ memoryUsage {{ {full} }} }}")
    await _assert_same(edge, '{ memoryActivities(memoryId: "mem-fact") { id accessedAt score } }')


@pytest.mark.parametrize("type_name, raw", [
    ("Artifact", "a-crlf"), ("Automation", "au-and"), ("AutomationRun", "r-err"),
    ("Workflow", "w2"), ("WorkflowRun", "wr1"), ("NotificationChannel", "n1"), ("Skill", "s2"),
])
async def test_node_resolves_every_type(edge, domains, type_name, raw):
    await _assert_same(edge, "query($id: ID!) { node(id: $id) { __typename id } }", {"id": _gid(type_name, raw)})


# ── mutations ────────────────────────────────────────────────────────────────
#
# A mutation runs on the edge over a byte-identical copy of the test database
# (`twin`), and its response, every table and every artifact file are compared
# with what Python's left on the original. Only what is generated fresh — uuid
# ids and "now" timestamps — is masked.

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2})?$")
# An `isoformat()` stamp inside a longer string — a JSON document's field.
_EMBEDDED_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00")


# Seeds dated relative to now (a run "a day ago") are this far back at most.
RECENT = timedelta(days=30)


def _mask(value: Any, since: datetime, dirs: tuple[str, ...]) -> Any:
    """Replace what legitimately differs between the recording and this run:
    paths, fresh uuids, and stamps written during the test (`<now>`, at or after
    `since`) or seeded relative to its start (`<recent>`)."""
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
        stamp = datetime.fromisoformat(f"{m[1]}T{m[2]}{m[3] or ''}{m[4] or '+00:00'}")
        if stamp >= since:
            return "<now>"
        if stamp >= since - RECENT:
            return "<recent>"
    value = _EMBEDDED_STAMP.sub(lambda m: "<now>" if datetime.fromisoformat(m[0]) >= since else m[0], value)
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
    """The edge on a copy of the test database — the one Python's recorded
    answers were taken on."""

    def __init__(self, edge: httpx.AsyncClient, a_dir: Path, b_dir: Path):
        self.edge, self.a_dir, self.b_dir = edge, a_dir, b_dir
        self.dirs = (str(a_dir), str(b_dir))
        # Anything stamped during this session was written by the test — a
        # seed's default, or a mutation — at a time no recording can share.
        self.since = started()

    async def run(
        self, query: str, variables: dict[str, Any] | None = None, *, edge_variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run on the edge and diff the answer, every table and the artifact
        files against Python's. `edge_variables` is for ids each side minted
        for itself (a run's, a queued message's)."""
        since = self.since
        # Python's answer, every table after it and the artifact files, masked.
        python, tables, files = await recorded()

        edge_vars = variables if edge_variables is None else edge_variables
        resp = await self.edge.post("/graphql", json={"query": query, "variables": edge_vars or {}})
        assert resp.status_code == 200
        body = resp.json()
        edge = {"data": body.get("data")}
        if body.get("errors"):
            edge["errors"] = [{"message": e["message"], "path": e.get("path")} for e in body["errors"]]

        assert _mask(edge, since, self.dirs) == python, query
        b = _dump(self.b_dir / "database.db")
        for table in tables:
            assert _mask(b[table], since, self.dirs) == tables[table], f"{table} after {query}"
        assert _files(self.b_dir / "artifacts") == files, f"files after {query}"
        return python


@pytest.fixture
async def twin(seeded, domains, database: Path, work_dir: Path, tmp_path_factory, edge_binary: Path, one_zone):
    import sqlite3

    # One markdown artifact with a live file and no history (the v1
    # migration path).
    (work_dir / "artifacts" / "a-plain.md").write_bytes(b"old\r\nbody")
    insert(database, "artifacts", id="a-plain", title="Plain", filename=str(work_dir / "artifacts" / "a-plain.md"),
           created_at=_ts(2026, 2, 3), updated_at=_ts(2026, 2, 3))

    # As the edge's start would leave it, on both sides.
    startup_sweep(edge_binary, work_dir, work_dir / "database.db")
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


async def test_artifact_mutations(twin):
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


def _sql_edge(twin: Twin, sql: str, *args: Any) -> None:
    """A write to the edge's copy that Python's database had too when its
    answers were recorded — set-up the mutations then diff."""
    import sqlite3

    with contextlib.closing(sqlite3.connect(twin.b_dir / "database.db")) as conn:
        conn.execute(sql, args)
        conn.commit()


BUILTIN = "google_genai:gemma-4-31b-it"
BOARD_FIELDS = (
    "id title body status priority createdBy model skill blockedReason blockedKind failureCount summary "
    "resultMetadata conversationId runId parentIds childIds createdAt updatedAt startedAt finishedAt"
)


async def test_board_task_mutations(twin):
    """Every board write the UI makes, including the dispatch pass a ready
    card starts and the inbox question a status change closes."""
    gid = lambda raw: _gid("BoardTask", raw)  # noqa: E731
    # b-b's question in the inbox, and a conversation from b-a's run.
    _sql_edge(twin, "INSERT INTO approvals (id, source, kind, status, question, label, board_task_id, parent_id, "
                    "requested_at, updated_at) VALUES ('ap-b', 'board_task', 'input', 'pending', '?', 'Answer', "
                    "'b-b', 'boardtask_b-b', '2026-03-02 00:00:00.000000', '2026-03-02 00:00:00.000000')")
    _sql_edge(twin, "INSERT INTO approvals (id, source, kind, status, question, label, board_task_id, "
                    "requested_at, updated_at) VALUES ('ap-arch', 'board_task', 'input', 'pending', '?', 'Answer', "
                    "'b-arch', '2026-03-02 00:00:00.000000', '2026-03-02 00:00:00.000000')")
    _sql_edge(twin, "INSERT INTO conversations (id, title, model, surface, pinned, ephemeral, created_at) "
                    "VALUES ('boardtask_b-a', 'part a', 'm', 'task', 0, 0, '2026-03-01 01:00:00.000000')")
    _sql_edge(twin, "INSERT INTO messages (id, conversation_id, role, content, status, created_at) "
                    "VALUES ('bm1', 'boardtask_b-a', 'user', 'do a', 'done', '2026-03-01 01:00:00.000000')")

    create = f"mutation($input: BoardTaskInput!) {{ createBoardTask(input: $input) {{ {BOARD_FIELDS} }} }}"
    await twin.run(create, {"input": {"title": "parked", "start": False}})
    await twin.run(create, {"input": {"title": "go", "body": "b", "priority": 3, "model": BUILTIN, "skill": "s"}})
    await twin.run(create, {"input": {"title": "child", "parentIds": [gid("b-a"), gid("b-b")]}})
    await twin.run(create, {"input": {"title": "orphan", "parentIds": [gid("zz"), gid("b-a"), gid("aa")]}})
    await twin.run(create, {"input": {"title": "bad", "model": "nope:x"}})

    update = f"mutation($id: ID!, $input: BoardTaskUpdateInput!) {{ updateBoardTask(id: $id, input: $input) {{ {BOARD_FIELDS} }} }}"
    await twin.run(update, {"id": gid("b-root"), "input": {"title": "ship it", "parentIds": [gid("b-a"), gid("b-a")]}})
    await twin.run(update, {"id": gid("b-root"), "input": {"priority": 7, "parentIds": [gid("b-root")]}})
    await twin.run(update, {"id": gid("b-a"), "input": {"parentIds": [gid("b-root")]}})  # a cycle
    await twin.run(update, {"id": gid("b-a"), "input": {"parentIds": [gid("nope")]}})
    await twin.run(update, {"id": gid("b-b"), "input": {"model": BUILTIN}})  # still asking: the question stays
    await twin.run(update, {"id": gid("b-arch"), "input": {"body": None}})   # no fields: still a bump
    await twin.run(update, {"id": gid("b-root"), "input": {"model": "nope:x"}})
    await twin.run(update, {"id": gid("nope"), "input": {"title": "x"}})

    move = f"mutation($id: ID!, $s: String!) {{ setBoardTaskStatus(id: $id, status: $s) {{ {BOARD_FIELDS} }} }}"
    await twin.run(move, {"id": gid("b-root"), "s": "running"})
    await twin.run(move, {"id": gid("nope"), "s": "done"})
    await twin.run(move, {"id": gid("b-arch"), "s": "todo"})   # its question closes
    await twin.run(move, {"id": gid("b-root"), "s": "ready"})  # dispatched
    _sql_edge(twin, "UPDATE board_tasks SET status = 'running' WHERE id = 'b-arch'")
    await twin.run(move, {"id": gid("b-arch"), "s": "done"})

    answer = f"mutation($id: ID!, $a: String!) {{ answerBoardTask(id: $id, answer: $a) {{ {BOARD_FIELDS} }} }}"
    await twin.run(answer, {"id": gid("b-b"), "a": "   "})
    await twin.run(answer, {"id": gid("b-a"), "a": "x"})
    await twin.run(answer, {"id": gid("nope"), "a": "x"})
    await twin.run(answer, {"id": gid("b-b"), "a": "  Green  "})  # answered, ready, dispatched

    delete = "mutation($id: ID!) { deleteBoardTask(id: $id) }"
    await twin.run(delete, {"id": gid("b-arch")})  # running
    await twin.run(delete, {"id": gid("b-a")})     # links both ways and its conversation go
    await twin.run(delete, {"id": gid("b-a")})


async def test_conversation_deletes_and_model_change(twin):
    """A conversation goes with everything it owns: messages and steps,
    artifacts with their versions and files, episodes, and its transcript — a blob another thread still names stays."""
    shared, own = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    _sql_edge(twin, "INSERT INTO messages (id, conversation_id, role, content, status, created_at) "
                    "VALUES ('cm1', 'c1', 'assistant', 'x', 'done', '2026-02-01 00:00:00.000000')")
    _sql_edge(twin, "INSERT INTO steps (id, message_id, conversation_id, node, source, seq, created_at) "
                    "VALUES ('cs1', 'cm1', 'c1', 'model', 'main', 0, '2026-02-01 00:00:00.000000')")
    _sql_edge(twin, "INSERT INTO conversation_episodes (id, conversation_id, text, created_at) "
                    "VALUES ('ep1', 'c1', 'earlier', '2026-02-01 00:00:00.000000')")
    for tid, seq, blob in (("c1", 0, shared), ("c1", 1, own), ("c-old", 0, shared)):
        data = json.dumps({"v": 1, "role": "user", "content": [{"type": "image", "blob": blob}]})
        _sql_edge(twin, "INSERT INTO thread_messages (id, thread_id, seq, message_id, role, data, created_at) "
                        "VALUES (?, ?, ?, ?, 'user', ?, '2026-02-01 00:00:00.000000')",
                  f"t-{tid}-{seq}", tid, seq, f"m-{seq}", data)
    for blob in (shared, own):
        _sql_edge(twin, "INSERT INTO transcript_blobs (hash, mime_type, size, data, created_at) "
                        "VALUES (?, 'image/png', 1, x'00', '2026-02-01 00:00:00.000000')", blob)
    _sql_edge(twin, "INSERT INTO thread_state (thread_id, todos, updated_at) VALUES ('c1', '[]', '2026-02-01 00:00:00.000000')")
    # A version file no row names: swept by its name.
    for d in (twin.a_dir, twin.b_dir):
        (d / "artifacts" / "a-crlf_v9.md").write_text("stray")

    delete = "mutation($id: ID!) { deleteConversation(id: $id) }"
    await twin.run(delete, {"id": _gid("Conversation", "c1")})
    await twin.run(delete, {"id": _gid("Conversation", "c1")})

    discard = "mutation($id: ID!) { discardConversation(id: $id) }"
    await twin.run(discard, {"id": _gid("Conversation", "c-old")})   # not incognito: kept
    await twin.run(discard, {"id": _gid("Conversation", "c-ghost")})
    await twin.run(discard, {"id": _gid("Conversation", "c-ghost")})

    update = "mutation($id: ID!, $m: String, $t: String) { updateConversation(id: $id, model: $m, title: $t) { id title model } }"
    c = _gid("Conversation", "c-old")
    await twin.run(update, {"id": c, "m": BUILTIN})
    await twin.run(update, {"id": c, "m": "nope:x", "t": "x"})
    await twin.run(update, {"id": c, "m": ""})
    await twin.run(update, {"id": _gid("Conversation", "nope"), "m": BUILTIN})


AUTOMATION_FIELDS = ("id name description inputType promptText model codeText webhookUrl webhookMethod webhookHeaders "
                     "webhookBody schedule enabled stateful notifications conversationId nextRunAt lastRunStatus "
                     "createdAt updatedAt")


async def test_automation_mutations(twin):
    """Create, update (every field written: one left out is cleared) and a
    human's delete, which takes the runs and a stateful automation's
    conversation with it. Validation refuses an unknown model and a schedule
    the scheduler can't build — the edge's cron engine, not APScheduler, now."""
    gid = lambda raw: _gid("Automation", raw)  # noqa: E731
    _sql_edge(twin, "INSERT INTO conversations (id, title, model, surface, pinned, ephemeral, created_at) "
                    "VALUES ('automation_au-off', 'disabled', 'm', 'automation', 0, 0, '2026-01-03 00:00:00.000000')")
    _sql_edge(twin, "INSERT INTO messages (id, conversation_id, role, content, status, created_at) "
                    "VALUES ('am1', 'automation_au-off', 'user', 'run', 'done', '2026-01-03 00:00:00.000000')")
    _sql_edge(twin, "INSERT INTO automation_runs (id, automation_id, status, triggered_by, started_at) "
                    "VALUES ('r-off', 'au-off', 'done', 'manual', '2026-01-03 00:00:00.000000')")

    create = f"mutation($input: AutomationInput!) {{ createAutomation(input: $input) {{ {AUTOMATION_FIELDS} }} }}"
    await twin.run(create, {"input": {"name": "bare", "inputType": "prompt"}})
    await twin.run(create, {"input": {
        "name": "full", "inputType": "webhook", "description": "d", "webhookUrl": "http://x", "webhookMethod": "PUT",
        "webhookHeaders": '{"a": "b"}', "webhookBody": "{}", "schedule": "0 9 * * 1-5", "enabled": False,
        "stateful": True, "notifications": "[]",
    }})
    await twin.run(create, {"input": {"name": "scheduled", "inputType": "monitor", "promptText": "watch",
                                      "model": BUILTIN, "schedule": "*/15 * * * *"}})
    await twin.run(create, {"input": {"name": "empty schedule", "inputType": "code", "codeText": "1", "schedule": ""}})
    for bad in ("nope", "60 * * * *", "0 9 * * 8", "* * * *"):
        await twin.run(create, {"input": {"name": "bad", "inputType": "prompt", "schedule": bad}})
    await twin.run(create, {"input": {"name": "bad", "inputType": "prompt", "model": "nope:x", "schedule": "nope"}})

    update = f"mutation($id: ID!, $input: AutomationInput!) {{ updateAutomation(id: $id, input: $input) {{ {AUTOMATION_FIELDS} }} }}"
    await twin.run(update, {"id": gid("au-hook"), "input": {"name": "hook 2", "inputType": "webhook"}})  # rest cleared
    await twin.run(update, {"id": gid("au-and"), "input": {"name": "x", "inputType": "prompt", "promptText": "p",
                                                           "schedule": "0 0 * * sun", "model": BUILTIN}})
    await twin.run(update, {"id": gid("au-and"), "input": {"name": "x", "inputType": "prompt", "schedule": "*/0 * * * *"}})
    await twin.run(update, {"id": gid("au-and"), "input": {"name": "x", "inputType": "prompt", "model": "nope:x"}})
    await twin.run(update, {"id": gid("nope"), "input": {"name": "x", "inputType": "prompt"}})

    delete = "mutation($id: ID!) { deleteAutomation(id: $id) }"
    await twin.run(delete, {"id": gid("au-off")})   # its runs and its conversation go
    await twin.run(delete, {"id": gid("au-and")})   # runs, no conversation
    await twin.run(delete, {"id": gid("au-and")})


async def test_a_failing_embedder_fails_memory_writes(database, work_dir: Path, edge_binary: Path):
    """A memory item can't be saved unembedded: the write fails with the
    embedder's error, before anything is written. A skill saves unembedded,
    as Python's does."""
    from edge_support import _free_port

    dead = {"OLLAMA_HOST": f"http://127.0.0.1:{_free_port()}"}
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", dead) as client:
        add = 'mutation { addMemory(text: "tea at noon") { id } }'
        failed = await client.post("/graphql", json={"query": add})
        assert failed.status_code == 200
        assert failed.json()["errors"][0]["message"].startswith("embedding failed:")
        create = 'mutation { createSkill(input: {name: "n", description: "d", body: "b"}) { name } }'
        assert (await client.post("/graphql", json={"query": create})).json() == {"data": {"createSkill": {"name": "n"}}}
    import sqlite3

    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as c:
        assert c.execute("SELECT count(*) FROM memories").fetchone() == (0,)
        assert c.execute("SELECT embedding FROM skills WHERE name = 'n'").fetchall() == [(None,)]


async def test_a_decomposition_is_checked_before_planning(edge, domains):
    """`decomposeBoardTask` refuses a task that isn't waiting before any
    model call, as Python refused it."""
    q = 'mutation($id: ID!) { decomposeBoardTask(id: $id) { id } }'
    done = await _edge(edge, q, {"id": _gid("BoardTask", "b-a")})
    assert done.json()["errors"][0]["message"] == "only waiting (todo/ready/blocked) tasks can be decomposed"


async def test_a_consolidation_of_an_unknown_project_is_refused(edge, seeded):
    q = "mutation($id: ID!) { consolidateProjectMemory(id: $id) }"
    refused = await _edge(edge, q, {"id": _gid("Project", "nope")})
    assert refused.json()["errors"][0]["message"] == "project not found"


async def test_agent_memory_blob(twin):
    """The free-text `AGENTS.md` blob in `kv_store`: read (a list content
    joined), the pre-fix `/AGENTS.md` key copied over on first touch, replaced
    keeping its `created_at`, deleted — after which the legacy copy comes back,
    as in Python."""
    fields = "content exists modifiedAt"
    read = _relay_text("AgentMemoryQuery")
    update = f"mutation($c: String!) {{ updateMemory(content: $c) {{ {fields} }} }}"
    delete = f"mutation {{ deleteAgentMemory {{ {fields} }} }}"
    await twin.run(read)
    await twin.run(update, {"c": "first"})  # no blob: created now
    await twin.run(delete)
    legacy = json.dumps({"content": ["- likes tea", "- café"], "created_at": "2025-01-01T00:00:00+00:00",
                         "modified_at": "2025-02-01T00:00:00+00:00"})
    _sql_edge(twin, "INSERT INTO kv_store (namespace, key, value, created_at, updated_at) "
                    "VALUES ('memory', '/AGENTS.md', ?, '2025-01-01 00:00:00', '2025-01-01 00:00:00')", legacy)
    await twin.run(read)
    await twin.run(update, {"c": "- likes tea\n- naïve ✓"})
    await twin.run(read)
    await twin.run(delete)
    await twin.run(read)  # the legacy copy, again
    await twin.run(update, {"c": ""})
    await twin.run(read)


async def test_setting_reads_and_writes(twin, monkeypatch):
    """The generic settings editor: the inventory (known keys unset, a
    free-form key, endpoint keys redacted), one key, and writes — the row a
    write returns carries Python's in-memory, UTC-aware stamp."""
    endpoints = json.dumps([{"name": "lab", "base_url": "http://x/v1", "api_key": "sk-secret"},
                            {"name": "local", "base_url": "http://y/v1", "api_key": ""}])
    for key, value in (("zeta.custom", "1"), ("alpha.custom", "ü"), ("models.endpoints", endpoints),
                       ("telegram.allowed_users", "42")):
        _sql_edge(twin, "INSERT INTO config_settings (key, value, updated_at) VALUES (?, ?, '2026-01-02 03:04:05.000000')",
                  key, value)
    await twin.run(_relay_text("SettingsQuery"))
    one = "query($k: String!) { setting(key: $k) { id key value updatedAt isSet label kind choices known } }"
    for key in ("models.endpoints", "browser.cdp_url", "nope", " telegram.allowed_users"):
        await twin.run(one, {"k": key})

    set_ = _relay_text("SetSettingMutation").replace("note", "note setting { id key value updatedAt isSet }", 1)
    for key, value in (
        (" brand.new ", "v"),                     # inserted, key stripped
        ("telegram.allowed_users", "1,2"),        # updated
        ("embedding.model", "models/x"),          # applied in Python's process too
        ("scheduler.timezone", "Europe/Paris"),   # restart required
        ("", "v"),
        ("a b", "v"),
        ("tools.policy", "{}"),                   # managed
        ("mcp.default_load_mode", "sometimes"),   # refused before the managed check
    ):
        await twin.run(set_, {"key": key, "value": value, "allowManaged": False})

    # The mask above hides it: the written row's stamp is aware, the rest naive.
    body = (await twin.edge.post("/graphql", json={"query": set_, "variables": {
        "key": "brand.new", "value": "w", "allowManaged": False}})).json()["data"]["setSetting"]
    assert body["setting"]["updatedAt"].endswith("+00:00")
    assert not any(r["updatedAt"].endswith("+00:00") for r in body["settings"] if r["isSet"] and r["key"] != "brand.new")
    await twin.run(set_, {"key": "brand.new", "value": "w", "allowManaged": False})

    delete = _relay_text("DeleteSettingMutation").replace("note", "note setting { id key value updatedAt isSet }", 1)
    for key in (" zeta.custom ", "zeta.custom", "embedding.model", "models.endpoints"):
        await twin.run(delete, {"key": key, "allowManaged": False})


async def test_managed_settings_overridden(twin):
    """`allowManaged: true` writes another tab's key (`mcp.*` keys:
    `test_edge_mcp.py`)."""
    set_ = "mutation($k: String!, $v: String!) { setSetting(key: $k, value: $v, allowManaged: true) { note setting { key value } } }"
    for key, value in (("tools.policy", "{}"), ("models.custom", "[]"), ("default.model", "ollama:x")):
        await twin.run(set_, {"k": key, "v": value})
    delete = "mutation($k: String!) { deleteSetting(key: $k, allowManaged: true) { note } }"
    for key in ("models.custom", "default.model"):
        await twin.run(delete, {"k": key})


async def test_settings_refuse_invalid_json(edge, seeded, work_dir: Path):
    """Invalid JSON for a json key is refused with the parser's reason, and
    nothing is written."""
    import sqlite3

    set_ = "mutation($k: String!, $v: String!, $a: Boolean!) { setSetting(key: $k, value: $v, allowManaged: $a) { note } }"
    body = (await _edge(edge, set_, {"k": "tools.policy", "v": "{", "a": True})).json()
    assert body["errors"][0]["message"].startswith("tools.policy must be valid JSON: ")
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as c:
        assert c.execute("SELECT count(*) FROM config_settings WHERE key = 'tools.policy'").fetchone() == (0,)
    # Two writes in one operation are both answered.
    both = 'mutation { setSetting(key: "a", value: "b") { note } deleteSetting(key: "a") { note } }'
    assert "errors" not in (await _edge(edge, both)).json()


CATALOG = "{ default providers discoverableProviders available { id label provider builtin contextWindow } endpoints { name baseUrl hasKey } }"


async def test_model_catalog_writes(twin, monkeypatch):
    """Custom models and endpoints: the `models.custom` / `models.endpoints`
    rows rewritten as Python's `json.dumps` writes them (junk rows dropped, an
    upsert moved last, a window kept or converted), and every refusal."""
    custom = json.dumps([
        {"id": "ollama:kept", "label": "Kept", "provider": "ollama", "context_window": "8000"},
        "junk",
        {"id": "lab:served", "label": "On lab"},
        {"id": "ollama:café", "label": "Café ✓", "provider": "ollama", "extra": [1.5, None]},
    ])
    endpoints = json.dumps([{"name": "lab", "base_url": "http://lab/v1/", "api_key": "sk-1"}, {"name": "Bad"}])
    for key, value in (("models.custom", custom), ("models.endpoints", endpoints)):
        _sql_edge(twin, "INSERT INTO config_settings (key, value, updated_at) VALUES (?, ?, '2026-01-02 03:04:05.000000')",
                  key, value)

    add = f"mutation($id: String!, $label: String!, $provider: String) {{ addModel(id: $id, label: $label, provider: $provider) {CATALOG} }}"
    update = add.replace("addModel", "updateModel")
    for vars_ in (
        {"id": " ollama:new ", "label": "  "},          # label falls back to the id
        {"id": "x:on-lab", "label": "L", "provider": " lab "},
        {"id": "nocolon", "label": "x"},
        {"id": "ollama:", "label": "x"},
        {"id": "zzz:m", "label": "x"},
        {"id": BUILTIN, "label": "x"},
        {"id": "ollama:kept", "label": "x"},
    ):
        await twin.run(add, vars_)
    for vars_ in ({"id": "ollama:kept", "label": "Renamed"}, {"id": BUILTIN, "label": "x"}, {"id": "ollama:nope", "label": "x"}):
        await twin.run(update, vars_)

    discovered = f"mutation($m: [DiscoveredModelInput!]!) {{ addDiscoveredModels(models: $m) {CATALOG} }}"
    for batch in (
        [],
        [{"id": "ollama:a", "label": "A", "contextWindow": 4096}, {"id": "ollama:new", "label": "", "provider": None}],
        [{"id": "ollama:b", "label": "B"}, {"id": "ollama:c", "label": "C", "contextWindow": 0}],
        [{"id": "ollama:b", "label": "B"}, {"id": BUILTIN, "label": "x"}],
        [{"id": "ollama:café", "label": "Café", "contextWindow": 9000}],
    ):
        await twin.run(discovered, {"m": batch})

    default = f"mutation($id: String!) {{ setDefaultModel(id: $id) {CATALOG} }}"
    remove = f"mutation($id: String!) {{ removeModel(id: $id) {CATALOG} }}"
    await twin.run(default, {"id": "ollama:a"})
    await twin.run(default, {"id": "ollama:gone"})
    await twin.run(remove, {"id": "ollama:a"})  # the default goes back to the seed
    for id_ in (BUILTIN, "ollama:a", " ollama:new"):
        await twin.run(remove, {"id": id_})

    add_ep = f"mutation($n: String!, $u: String!, $k: String) {{ addEndpoint(name: $n, baseUrl: $u, apiKey: $k) {CATALOG} }}"
    for vars_ in (
        {"n": " grp ", "u": " https://grp/v1// ", "k": " sk-ü "},
        {"n": "keyless", "u": "http://local:1234/v1", "k": "  "},
        {"n": "Bad", "u": "http://x"},
        {"n": "ollama", "u": "http://x"},
        {"n": "lab", "u": "http://x"},
        {"n": "ftp", "u": "ftp://x"},
    ):
        await twin.run(add_ep, vars_)
    update_ep = (f"mutation($n: String!, $u: String!, $k: String, $c: Boolean!) "
                 f"{{ updateEndpoint(name: $n, baseUrl: $u, apiKey: $k, clearKey: $c) {CATALOG} }}")
    for vars_ in (
        {"n": "lab", "u": "https://lab2/v1/", "k": None, "c": False},   # key kept
        {"n": "lab", "u": "https://lab2/v1", "k": "new", "c": True},    # cleared wins
        {"n": "keyless", "u": "http://local:1234/v1", "k": " k ", "c": False},
        {"n": "nope", "u": "http://x", "c": False},
        {"n": "lab", "u": "x", "c": False},
    ):
        await twin.run(update_ep, vars_)
    remove_ep = f"mutation($n: String!) {{ removeEndpoint(name: $n) {CATALOG} }}"
    await twin.run(remove_ep, {"n": "lab"})  # lab:served and x:on-lab use it
    await twin.run(remove, {"id": "lab:served"})
    await twin.run(remove, {"id": "x:on-lab"})
    await twin.run(remove_ep, {"n": "lab"})
    await twin.run(remove_ep, {"n": "lab"})


def test_the_edge_lists_the_sdk_catalogue():
    """`edge/src/gql/sdk_tools.json` is the `jarvis` SDK as the tool inventory
    lists it: each discoverable function, its docstring's first line, its
    category. Re-export with `JARVIS_UPDATE_GOLDEN=1 uv run pytest
    tests/test_edge_parity.py -k catalogue` after changing an SDK function's
    name, category or docstring, then rebuild the edge."""
    import inspect
    import os

    from tools import sdk

    python = [
        {"name": fn.__name__, "description": ((inspect.getdoc(fn) or "").strip().splitlines() or [""])[0], "group": category}
        for category, (_blurb, funcs) in sdk._CATEGORIES.items()
        for fn in funcs
    ]
    path = ROOT / "edge" / "src" / "gql" / "sdk_tools.json"
    if os.environ.get("JARVIS_UPDATE_GOLDEN") == "1":
        path.write_text(json.dumps(python, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == python


async def test_tool_inventory_and_policy(twin):
    """The Tools page: the inventory with its policy, and `setToolPolicy`
    keeping only non-default entries, in the stored order."""
    stored = json.dumps({"sdk:zzz": {"enabled": False, "extra": 1.0}, "bound:remember": {"approval": "yes"}, "weird": 3})
    _sql_edge(twin, "INSERT INTO config_settings (key, value, updated_at) VALUES ('tools.policy', ?, '2026-01-02 03:04:05.000000')",
              stored)
    await twin.run(_relay_text("ToolsQuery"))
    policy = _relay_text("SetToolPolicyMutation")
    for vars_ in (
        {"key": "sdk:list_artifacts", "enabled": False},
        {"key": "bound:run_cell", "requiresApproval": True},
        {"key": "bound:remember", "requiresApproval": False},  # back to the default: dropped
        {"key": "sdk:list_artifacts", "enabled": None, "requiresApproval": True},
        {"key": "mcp:srv/tool", "enabled": False},
        {"key": "nope"},
        {"key": "bound:"},
    ):
        await twin.run(policy, vars_)


async def test_an_unreadable_catalog_refuses_writes(edge, seeded, work_dir: Path):
    """A `models.custom` row that isn't a model, or a stored window that isn't
    a whole number, refuses the write with the reason — before anything is
    written."""
    import sqlite3

    def put(key: str, value: str) -> None:
        with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as c:
            c.execute("INSERT OR REPLACE INTO config_settings (key, value, updated_at) VALUES (?, ?, '2026-01-01 00:00:00')",
                      (key, value))
            c.commit()

    put("models.custom", json.dumps([{"id": "ollama:w", "label": "W", "context_window": "lots"}]))
    q = 'mutation { updateModel(id: "ollama:w", label: "x") { default } }'
    assert "context_window" in (await _edge(edge, q)).json()["errors"][0]["message"]
    put("models.custom", json.dumps([{"id": "ollama:w", "label": 5}]))
    body = (await _edge(edge, 'mutation { addModel(id: "ollama:y", label: "y") { default } }')).json()
    assert body["errors"][0]["message"].startswith("the custom models setting (models.custom) can't be read: ")
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as c:
        stored = c.execute("SELECT value FROM config_settings WHERE key = 'models.custom'").fetchone()
    assert json.loads(stored[0]) == [{"id": "ollama:w", "label": 5}]


async def test_an_agents_deletes_wait_for_approval(edge, domains, work_dir: Path):
    """With `approval.required_actions` gating them, an agent's delete is
    recorded for a human instead of performed — once, however often it is
    retried — and runs when approved. A human's delete is its own approval."""
    import sqlite3

    agent = {"X-Jarvis-Caller": "agent"}
    missing = f'mutation {{ deleteWorkflow(id: "{_gid("Workflow", "nope")}") }}'
    body = (await edge.post("/graphql", json={"query": missing}, headers=agent)).json()
    assert body["errors"][0]["message"] == "workflow not found"

    await _edge(edge, 'mutation { setSetting(key: "approval.required_actions", value: "all") { note } }')
    delete = 'mutation($id: ID!) { deleteSkill(id: $id) }'
    skill = _gid("Skill", "s1")
    asked = [(await edge.post("/graphql", json={"query": delete, "variables": {"id": skill}}, headers=agent)).json()
             for _ in range(2)]
    for body in asked:
        assert body["errors"][0]["message"].startswith("Approval required: Delete skill ")
    assert asked[0] == asked[1]
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as c:
        [(approval_id, action, payload)] = c.execute(
            "SELECT id, action, action_payload FROM approvals WHERE status = 'pending' AND action = 'delete_skill'").fetchall()
        assert c.execute("SELECT count(*) FROM skills WHERE id = 's1'").fetchone() == (1,)
    assert (action, json.loads(payload)["skill_id"]) == ("delete_skill", "s1")

    resolve = 'mutation($id: String!) { resolveApproval(id: $id, answer: "yes") { status result } }'
    done = (await _edge(edge, resolve, {"id": approval_id})).json()
    assert done == {"data": {"resolveApproval": {"status": "approved", "result": "Deleted."}}}
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as c:
        assert c.execute("SELECT count(*) FROM skills WHERE id = 's1'").fetchone() == (0,)


# ── routing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        "{ notARootField { ready } }",
        "{ conversations { id } notARootField { ready } }",
        'mutation { notAMutation { ready } }',
    ],
)
async def test_unknown_fields_are_validation_errors(edge, seeded, query):
    resp = await _edge(edge, query)
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] is None and "Unknown field" in body["errors"][0]["message"]


@pytest.mark.parametrize(
    "request_",
    [
        {"content": b'{"query": "{ __typename }"}', "headers": {"content-type": "text/plain"}},
        {"content": b'{"query": "{ __typename }"}', "headers": {"content-type": "Application/JSON"}},
        {"files": {"operations": (None, '{"query": "{ __typename }"}'), "map": (None, "{}")}},
        {"json": [{"query": "{ __typename }"}]},
        {"content": b"{nope", "headers": {"content-type": "application/json"}},
        {"json": {"variables": {}}},
        {"json": {"query": None}},
        {"json": {"query": 5}},
        {"json": {"query": "{ __typename }", "variables": [1]}},
        {"json": {"query": "{ __typename }", "extensions": "x"}},
        {"json": {"query": "query A { conversations { id } }", "operationName": "B"}},
        {"json": {"query": "{ conversations { id } }", "operationName": "A"}},
        {"json": {"query": "query A { conversations { id } }", "operationName": 5}},
    ],
)
async def test_requests_strawberry_refuses_are_refused(edge, seeded, request_):
    """What Strawberry refused before executing gets its 400 and its words."""

    expected = await recorded()  # (status, body)
    resp = await edge.post("/graphql", **request_)
    assert expected[0] == 400
    assert (resp.status_code, resp.text) == expected


@pytest.mark.parametrize(
    "query",
    [
        "{ conversations ",                       # a syntax error
        "{ conversations { id notAField } }",     # an owned root field, a field no type has
        'subscription { taskEvents(taskId: "x") { __typename } }',  # over HTTP
    ],
)
async def test_errors_executing_reports_are_answered_here(edge, seeded, query):
    resp = await _edge(edge, query)
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] is None and body["errors"]


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


def test_the_frontend_compiles_against_this_schema(edge_binary):
    """`frontend/schema.graphql` is the server's schema; `pnpm schema`
    re-exports it after a change."""
    printed = subprocess.run([str(edge_binary), "--print-schema"], capture_output=True, text=True, check=True).stdout
    assert (ROOT / "frontend" / "schema.graphql").read_text() == printed, "run `pnpm schema` in frontend/"


async def test_the_schema_keeps_every_field_python_had(edge_binary):
    """Every type, field and argument the Python schema had is still here
    with the same type — the frontend, the bots and the SDK were written
    against it."""
    from graphql import GraphQLInputObjectType, GraphQLInterfaceType, GraphQLObjectType, build_schema

    rust = build_schema(subprocess.run([str(edge_binary), "--print-schema"], capture_output=True, text=True, check=True).stdout)
    python = build_schema(await recorded())  # Python's SDL

    for name, ptype in python.type_map.items():
        if name.startswith("__"):
            continue
        rtype = rust.type_map.get(name)
        if isinstance(ptype, GraphQLInputObjectType):
            assert isinstance(rtype, GraphQLInputObjectType), f"input {name} is gone"
            assert {f: _input_signature(v) for f, v in rtype.fields.items()} == {
                f: _input_signature(v) for f, v in ptype.fields.items()
            }, f"input {name} differs"
            continue
        if not isinstance(ptype, (GraphQLObjectType, GraphQLInterfaceType)):
            continue
        assert rtype is not None, f"{name} is gone"
        for fname, pfield in ptype.fields.items():
            assert fname in rtype.fields, f"{name}.{fname} is gone"
            assert _signature(rtype.fields[fname]) == _signature(pfield), f"{name}.{fname} differs"


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
