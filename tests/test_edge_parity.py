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
        # Mutations always go to Python in this phase.
        'mutation { deleteConversation(id: "x") }',
        # A node id of a type the edge can't resolve.
        '{ node(id: "V29ya2Zsb3c6YWJj") { id } }',
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
