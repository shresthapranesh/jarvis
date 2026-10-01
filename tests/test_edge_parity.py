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
import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
EDGE_DIR = ROOT / "edge"
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
}


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def edge_binary() -> Path:
    if shutil.which("cargo") is None:
        pytest.skip("cargo not installed")
    subprocess.run(
        ["cargo", "build", "--quiet", "--manifest-path", str(EDGE_DIR / "Cargo.toml")],
        check=True,
    )
    return EDGE_DIR / "target" / "debug" / "jarvis-edge"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def edge(database, work_dir: Path, edge_binary: Path):
    port, dead = _free_port(), _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{work_dir}/database.db",
        "JARVIS_EDGE_BIND": f"127.0.0.1:{port}",
        # Nothing listens here: a proxied operation fails loudly.
        "JARVIS_BACKEND_URL": f"http://127.0.0.1:{dead}",
        "JARVIS_EDGE_LOG": "warn",
    }
    # cwd = work_dir so the edge's .env lookup can't find the repo's .env.
    proc = subprocess.Popen([str(edge_binary)], env=env, cwd=work_dir)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            await asyncio.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("edge did not start")
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            yield client
    finally:
        proc.terminate()
        proc.wait(timeout=5)


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


def _gid(type_name: str, raw: str) -> str:
    from strawberry.relay.utils import to_base64

    return to_base64(type_name, raw)


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


def test_edge_schema_is_a_subset_of_python(edge_binary):
    """Every type and field the edge defines exists in Python with the same
    type and arguments — the edge may lag Python, never contradict it."""
    from graphql import GraphQLInterfaceType, GraphQLObjectType, build_schema

    from server.graphql.schema import schema

    rust = build_schema(subprocess.run([str(edge_binary), "--print-schema"], capture_output=True, text=True, check=True).stdout)
    python = build_schema(schema.as_str())

    for name, rtype in rust.type_map.items():
        if name.startswith("__") or not isinstance(rtype, (GraphQLObjectType, GraphQLInterfaceType)):
            continue
        ptype = python.type_map.get(name)
        assert ptype is not None, f"{name} is not in the Python schema"
        for fname, rfield in rtype.fields.items():
            assert fname in ptype.fields, f"{name}.{fname} is not in the Python schema"
            assert _signature(rfield) == _signature(ptype.fields[fname]), f"{name}.{fname} differs"
        # A non-root type is fully ported or not at all: a missing field would
        # make an owned operation fail validation and fall back on every call.
        if name != "Query":
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
