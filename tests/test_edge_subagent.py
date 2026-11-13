"""A sub-agent's conversation (`surface = 'subagent'`): its turns are
`subagent` jobs, answered by its role's prompt and tools on its own thread —
whoever wrote to it.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from edge_support import _free_port, _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture
from seed import execute, insert, row
from test_edge_agent import REPO, START, _finished
from test_edge_loop import MODEL, ROLE_PROMPTS, ByRole, FakeOllama, Reply, fake  # noqa: F401 — fake is a fixture

WRITER_TOOLS = ["read_file", "write_file", "write_artifact", "read_artifact", "list_artifacts"]


def _env(work_dir: Path, fake: FakeOllama) -> dict[str, str]:
    return {"JARVIS_RUN_JOBS": "1", "OLLAMA_HOST": fake.url, "JARVIS_APP_DIR": str(REPO), "HOME": str(work_dir),
            "JARVIS_BROWSER_CDP_URL": f"http://127.0.0.1:{_free_port()}"}


def _subagent(db: Path, role: str | None, conversation_id: str = "child") -> None:
    if row(db, "conversations", "parent") is None:
        # Turns run on the fake model.
        insert(db, "config_settings", key="models.custom", value=json.dumps([{"id": MODEL, "label": "Fake"}]))
        insert(db, "conversations", id="parent", title="p", model=MODEL)
    insert(db, "conversations", id=conversation_id, title="draft the summary", model=MODEL, surface="subagent",
           parent_id="parent", agent_role=role)


async def _send(client, conversation_id: str, query: str) -> str:
    resp = await client.post("/graphql", json={"query": START, "variables": {
        "input": {"query": query, "model": MODEL, "conversationId": conversation_id}}})
    body = resp.json()
    assert "errors" not in body, body
    return body["data"]["startTask"]["taskId"]


def _system(request: dict) -> str:
    first = request["messages"][0]
    assert first["role"] == "system"
    return first["content"]


async def test_a_sub_agent_answers_as_its_role_and_keeps_its_thread(database, work_dir: Path, edge_binary: Path,
                                                                     fake: FakeOllama):
    _subagent(database, "writer")
    # A writer runs no code: the call is answered, not run.
    fake.reset([Reply("", [("run_cell", {"code": "1"})]), Reply("The summary."), Reply("Shorter.")])
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _env(work_dir, fake)) as client:
        first = await _send(client, "child", "draft the summary")
        job = await _finished(database, first)
        assert (job["kind"], job["status"], job["thread_id"]) == ("subagent", "done", "child")
        assert json.loads(job["payload"])["role"] == "writer"
        message = row(database, "messages", first)
        assert (message["status"], message["content"], message["conversation_id"]) == ("done", "The summary.", "child")

        # Asked again, it goes on from what it did: the same conversation is re-used.
        second = await _send(client, "child", "make it shorter")
        job = await _finished(database, second)
        assert (job["kind"], job["status"]) == ("subagent", "done")
        assert row(database, "messages", second)["content"] == "Shorter."

        # It stays out of the sidebar's list.
        listed = (await client.post("/graphql", json={"query": "{ conversations { title } }"})).json()
        assert [c["title"] for c in listed["data"]["conversations"]] == ["p"]

    one, two, three = fake.requests
    for request in (one, two, three):
        # Its role's prompt and tools, not the main agent's.
        assert _system(request).startswith("You are a writing worker.")
        assert [t["function"]["name"] for t in request["tools"]] == WRITER_TOOLS
    refusal = two["messages"][-1]
    assert refusal["role"] == "tool" and "run_cell is not a valid tool" in refusal["content"]
    said = [(m["role"], m.get("content")) for m in three["messages"] if m["role"] in ("user", "assistant")]
    assert said == [("user", "draft the summary"), ("assistant", ""), ("assistant", "The summary."),
                    ("user", "make it shorter")]


async def test_a_sub_agent_without_a_role_is_a_general_one(database, work_dir: Path, edge_binary: Path,
                                                            fake: FakeOllama):
    _subagent(database, None)
    fake.reset([Reply("Done.")])
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _env(work_dir, fake)) as client:
        task = await _send(client, "child", "do it")
        assert (await _finished(database, task))["status"] == "done"
    [request] = fake.requests
    assert _system(request).startswith("You are a focused worker agent.")
    assert "run_cell" in [t["function"]["name"] for t in request["tools"]]


async def test_a_sub_agent_of_an_unknown_role_fails_saying_so(database, work_dir: Path, edge_binary: Path,
                                                               fake: FakeOllama):
    _subagent(database, "pirate")
    fake.reset([])
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _env(work_dir, fake)) as client:
        task = await _send(client, "child", "arr")
        job = await _finished(database, task)
    why = "the sub-agent job has no query, model, conversation or known role"
    assert (job["status"], job["last_error"]) == ("error", why)
    message = row(database, "messages", task)
    assert (message["status"], message["content"]) == ("error", f"The run failed before completing: {why}")
    assert fake.requests == []


# ── spawned by an agent ──────────────────────────────────────────────────────

STOP = "mutation($id: String!) { stopTask(taskId: $id) }"
RESOLVE = "mutation($id: String!, $a: String!) { resolveApproval(id: $id, answer: $a) { status } }"


def _run_agents(*tasks: dict, **more) -> Reply:
    return Reply("", [("run_agents", {"tasks": list(tasks), **more})])


def _rows(db: Path, sql: str, *args) -> list[dict]:
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args)]


def _tool_results(request: dict) -> list[str]:
    return [m["content"] for m in request["messages"] if m["role"] == "tool"]


def _main(requests: list[dict]) -> list[dict]:
    return [r for r in requests if not any(_system(r).startswith(p) for p in ROLE_PROMPTS.values())]


async def _chat(client, db: Path, query: str) -> tuple[str, str]:
    """A new chat on the fake model: its turn and its conversation."""
    if row(db, "config_settings", "models.custom", "key") is None:
        insert(db, "config_settings", key="models.custom", value=json.dumps([{"id": MODEL, "label": "Fake"}]))
    resp = await client.post("/graphql", json={"query": START, "variables": {"input": {"query": query, "model": MODEL}}})
    body = resp.json()
    assert "errors" not in body, body
    conversation = _rows(db, "SELECT conversation_id FROM messages WHERE id = ?", body["data"]["startTask"]["taskId"])
    return body["data"]["startTask"]["taskId"], conversation[0]["conversation_id"]


async def _until(check, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (got := check()):
            return got
        await asyncio.sleep(0.05)
    raise AssertionError("never happened")


async def test_an_agent_spawns_sub_agents_and_reads_their_answers(database, work_dir: Path, edge_binary: Path,
                                                                   fake: FakeOllama):
    fake.reset(ByRole(
        [_run_agents({"task": "Summarise the thing", "role": "writer", "context": "It is long"},
                     {"task": "Cook", "role": "chef"}),
         Reply("All done.")],
        writer=[Reply("The summary.")]))
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _env(work_dir, fake)) as client:
        task, conversation = await _chat(client, database, "delegate")
        assert (await _finished(database, task))["status"] == "done"

    [child] = _rows(database, "SELECT * FROM conversations WHERE surface = 'subagent'")
    assert (child["parent_id"], child["agent_role"], child["spawned_by"], child["title"]) == (
        conversation, "writer", task, "Summarise the thing")
    said = _rows(database, "SELECT role, content, status, source FROM messages WHERE conversation_id = ? ORDER BY rowid",
                 child["id"])
    assert [tuple(m.values()) for m in said] == [
        ("user", "Context: It is long\n\nTask: Summarise the thing", "done", "agent"),
        ("assistant", "The summary.", "done", None)]
    [job] = _rows(database, "SELECT kind, status, payload FROM jobs WHERE thread_id = ?", child["id"])
    assert (job["kind"], job["status"]) == ("subagent", "done")
    assert json.loads(job["payload"]) | {"deliver": False, "by": "agent", "role": "writer"} == json.loads(job["payload"])

    first, second = _main(fake.requests)
    assert [t["function"]["name"] for t in first["tools"]][4:6] == ["run_agents", "check_agents"]
    [answer] = _tool_results(second)
    assert answer == (f"Agent {child['id']} (writer) — done: Summarise the thing\nThe summary.\n\n---\n\n"
                      "Task: Cook\nERROR: Unknown role 'chef' (available: coder, general, researcher, writer)")
    assert row(database, "messages", task)["content"] == "All done."
    # Its usage counts against the run that waited for it.
    parent, sub = row(database, "messages", task), _rows(database, "SELECT * FROM messages WHERE conversation_id = ? AND role = 'assistant'", child["id"])[0]
    assert parent["output_tokens"] == 14 + sub["output_tokens"]


async def test_an_agent_gives_a_sub_agent_a_follow_up(database, work_dir: Path, edge_binary: Path, fake: FakeOllama):
    def follow_up(body: dict) -> Reply:
        agent_id = _tool_results(body)[-1].split(" ")[1]
        return _run_agents({"task": "Make it shorter", "agent_id": agent_id}, {"task": "Again", "agent_id": "nobody"})

    fake.reset(ByRole([_run_agents({"task": "Draft it", "role": "writer"}), follow_up, Reply("Done.")],
                      writer=[Reply("A long draft."), Reply("Short.")]))
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _env(work_dir, fake)) as client:
        task, _ = await _chat(client, database, "delegate")
        assert (await _finished(database, task))["status"] == "done"

    [child] = _rows(database, "SELECT id FROM conversations WHERE surface = 'subagent'")
    said = _rows(database, "SELECT content FROM messages WHERE conversation_id = ? ORDER BY rowid", child["id"])
    assert [m["content"] for m in said] == ["Draft it", "A long draft.", "Make it shorter", "Short."]
    # Its second task came with what it did for the first.
    writer = [r for r in fake.requests if _system(r).startswith(ROLE_PROMPTS["writer"])]
    assert [(m["role"], m["content"]) for m in writer[1]["messages"][1:]] == [
        ("user", "Draft it"), ("assistant", "A long draft."), ("user", "Make it shorter")]
    answer = _tool_results(_main(fake.requests)[2])[-1]
    assert answer == (f"Agent {child['id']} (writer) — done: Make it shorter\nShort.\n\n---\n\n"
                      "Task: Again\nERROR: no sub-agent of yours has the id nobody")


async def test_an_answer_nobody_waited_for_comes_as_a_message(database, work_dir: Path, edge_binary: Path,
                                                               fake: FakeOllama):
    def main(body: dict) -> Reply:
        if any("[Sub-agent" in (m.get("content") or "") for m in body["messages"] if m["role"] == "user"):
            return Reply("Got it.")
        if _tool_results(body):
            return Reply("Working on it.")
        return _run_agents({"task": "Summarise the thing", "role": "writer"}, wait=False)

    fake.reset(ByRole([main] * 6, writer=[Reply("The summary.")]))
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _env(work_dir, fake)) as client:
        task, conversation = await _chat(client, database, "delegate")
        await _until(lambda: _rows(database, "SELECT id FROM messages WHERE conversation_id = ? AND role = 'assistant' "
                                             "AND status = 'done' AND content LIKE '%Got it.'", conversation))

    [child] = _rows(database, "SELECT id FROM conversations WHERE surface = 'subagent'")
    started = _tool_results(_main(fake.requests)[1])[-1]
    assert started.startswith(f"Agent {child['id']} (writer) started: Summarise the thing\n\n")
    [delivered] = _rows(database, "SELECT content, source, role FROM messages WHERE conversation_id = ? AND source IS NOT NULL",
                        conversation)
    assert delivered == {"content": f"[Sub-agent {child['id']} (writer) finished: done]\nThe summary.",
                         "source": f"subagent:{child['id']}", "role": "user"}
    [job] = _rows(database, "SELECT payload FROM jobs WHERE thread_id = ?", child["id"])
    assert json.loads(job["payload"])["deliver"] is True


async def test_stopping_a_run_stops_its_sub_agents(database, work_dir: Path, edge_binary: Path, fake: FakeOllama):
    # The writer's call (the second request) is held: it is running when the stop comes.
    fake.reset(ByRole([_run_agents({"task": "Summarise the thing", "role": "writer"}), Reply("unreached")],
                      writer=[Reply("never said")]), hold=1)
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _env(work_dir, fake)) as client:
        task, _ = await _chat(client, database, "delegate")
        await fake.held()
        resp = await client.post("/graphql", json={"query": STOP, "variables": {"id": task}})
        assert "errors" not in resp.json(), resp.json()
        try:
            assert (await _finished(database, task))["status"] == "done"
            [child] = _rows(database, "SELECT id FROM jobs WHERE kind = 'subagent'")
            await _finished(database, child["id"])
        finally:
            fake.release()
    assert row(database, "messages", task)["status"] == "stopped"
    assert row(database, "messages", child["id"])["status"] == "stopped"
    assert len(_main(fake.requests)) == 1


async def test_a_sub_agent_asks_in_its_parents_chat_before_a_gated_tool(database, work_dir: Path, edge_binary: Path,
                                                                         fake: FakeOllama):
    """While its parent waits for it, that chat is the one someone is
    watching: the request names the parent's run and conversation."""
    insert(database, "config_settings", key="tools.policy",
           value=json.dumps({"bound:run_cell": {"enabled": True, "approval": True}}))
    fake.reset(ByRole([_run_agents({"task": "Compute it", "role": "coder"}), Reply("Done.")],
                      coder=[Reply("", [("run_cell", {"code": "6 * 7"})]), Reply("It is 42.")]))
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _env(work_dir, fake)) as client:
        task, conversation = await _chat(client, database, "compute")
        [asked] = await _until(lambda: _rows(database, "SELECT id, task_id, parent_id, tool FROM approvals "
                                                       "WHERE status = 'pending'"))
        assert (asked["task_id"], asked["parent_id"], asked["tool"]) == (task, conversation, "run_cell")
        resp = await client.post("/graphql", json={"query": RESOLVE, "variables": {"id": asked["id"], "a": "Approve"}})
        assert "errors" not in resp.json(), resp.json()
        assert (await _finished(database, task, timeout=60))["status"] == "done"
    answer = _tool_results(_main(fake.requests)[1])[-1]
    assert answer.endswith("(coder) — done: Compute it\nIt is 42.")
    coder = [r for r in fake.requests if _system(r).startswith(ROLE_PROMPTS["coder"])]
    assert "42" in _tool_results(coder[1])[-1]


async def test_a_sub_agents_file_tools(database, work_dir: Path, edge_binary: Path, fake: FakeOllama, tmp_path: Path):
    """A general sub-agent's files, a tool it isn't bound to and arguments
    it got wrong — answered for it, as the main agent's are."""
    notes = tmp_path / "notes"
    fake.reset(ByRole(
        [_run_agents({"task": "Take notes"}), Reply("Noted.")],
        general=[
            Reply("Filing. ", [("write_file", {"filepath": f"{notes}/a.txt", "content": "line one\nline two"})]),
            Reply("", [("list_files", {"directory": f"{notes}/"}), ("read_file", {"filepath": f"{notes}/a.txt"}),
                       ("read_file", {"filepath": 5}), ("run_workflow", {"workflow_id": "w"}),
                       ("list_artifacts", {"all_conversations": True})]),
            Reply("Done with the files."),
        ]))
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _env(work_dir, fake)) as client:
        task, _ = await _chat(client, database, "take notes for me")
        assert (await _finished(database, task))["status"] == "done"
    assert (notes / "a.txt").read_text() == "line one\nline two"
    general = [r for r in fake.requests if _system(r).startswith(ROLE_PROMPTS["general"])]
    listed, read, bad, unbound, artifacts = _tool_results(general[2])[-5:]
    assert "a.txt" in listed and read == "line one\nline two"
    assert bad.startswith("Error invoking tool 'read_file' with kwargs {'filepath': 5}")
    assert unbound.startswith("Error: run_workflow is not a valid tool, try one of [run_cell, read_file,")
    assert isinstance(artifacts, str)
    assert _tool_results(_main(fake.requests)[1])[-1].endswith("(general) — done: Take notes\nDone with the files.")


# ── over GraphQL ─────────────────────────────────────────────────────────────

SUBAGENTS = """query($id: ID!) { node(id: $id) { ... on Conversation {
  subagents { conversationId role title status taskId result spawnedBy }
  messages(last: 10) { edges { node { role source } } } } } }"""
CHILD = """query($id: ID!) { node(id: $id) { ... on Conversation {
  surface agentRole parent { title } messages(last: 10) { edges { node { role source content } } } } } }"""
DELETE = "mutation($id: ID!) { deleteConversation(id: $id) }"


def _gid(kind: str, raw: str) -> str:
    return base64.b64encode(f"{kind}:{raw}".encode()).decode()


async def _gql(client, query: str, **variables) -> dict:
    body = (await client.post("/graphql", json={"query": query, "variables": variables})).json()
    assert "errors" not in body, body
    return body["data"]


async def test_a_conversation_lists_its_sub_agents_and_deletes_them_with_it(database, work_dir: Path,
                                                                           edge_binary: Path, fake: FakeOllama):
    def main(body: dict) -> Reply:
        if any("[Sub-agent" in (m.get("content") or "") for m in body["messages"] if m["role"] == "user"):
            return Reply("Got it.")
        if _tool_results(body):
            return Reply("Working on it.")
        return _run_agents({"task": "Summarise the thing", "role": "writer"}, wait=False)

    asked, answer = threading.Event(), threading.Event()

    def writer(body: dict) -> Reply:  # held until the test has looked
        asked.set()
        answer.wait(30)
        return Reply("The summary.")

    fake.reset(ByRole([main] * 6, writer=[writer]))
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _env(work_dir, fake)) as client:
        task, conversation = await _chat(client, database, "delegate")
        assert await asyncio.to_thread(asked.wait, 30)
        try:
            [running] = (await _gql(client, SUBAGENTS, id=_gid("Conversation", conversation)))["node"]["subagents"]
        finally:
            answer.set()
        child = running["conversationId"]
        assert running == {"conversationId": child, "role": "writer", "title": "Summarise the thing",
                           "status": "running", "taskId": running["taskId"], "result": None, "spawnedBy": task}
        assert row(database, "jobs", running["taskId"])["thread_id"] == child

        await _until(lambda: _rows(database, "SELECT id FROM messages WHERE conversation_id = ? AND role = 'assistant' "
                                             "AND status = 'done' AND content LIKE '%Got it.'", conversation))
        parent = (await _gql(client, SUBAGENTS, id=_gid("Conversation", conversation)))["node"]
        [done] = parent["subagents"]
        assert (done["status"], done["result"]) == ("done", "The summary.")
        # The answer that came back is marked as the sub-agent's.
        assert {"role": "user", "source": f"subagent:{child}"} in [e["node"] for e in parent["messages"]["edges"]]

        sub = (await _gql(client, CHILD, id=_gid("Conversation", child)))["node"]
        assert (sub["surface"], sub["agentRole"], sub["parent"]) == ("subagent", "writer", {"title": "delegate"})
        assert [e["node"] for e in sub["messages"]["edges"]] == [
            {"role": "user", "source": "agent", "content": "Summarise the thing"},
            {"role": "assistant", "source": None, "content": "The summary."}]

        assert (await _gql(client, DELETE, id=_gid("Conversation", conversation))) == {"deleteConversation": True}
    assert _rows(database, "SELECT id FROM conversations") == []
    assert _rows(database, "SELECT id FROM messages") == []
    assert _rows(database, "SELECT id FROM thread_messages") == []


# ── what outlives a run ──────────────────────────────────────────────────────

TRIGGER = "mutation($id: ID!) { triggerAutomation(id: $id) }"


async def test_a_run_without_a_conversation_takes_its_sub_agents_with_it(database, work_dir: Path, edge_binary: Path,
                                                                        fake: FakeOllama):
    """An automation's run has nowhere to keep sub-agents: it waits for them
    whatever it asked, reads their answers, and deletes them as it ends."""
    insert(database, "config_settings", key="models.custom", value=json.dumps([{"id": MODEL, "label": "Fake"}]))
    insert(database, "automations", id="auto-1", name="Daily", input_type="prompt", prompt_text="Summarise the news",
           model=MODEL)
    fake.reset(ByRole([_run_agents({"task": "Summarise the thing", "role": "writer"}, wait=False), Reply("Filed.")],
                      writer=[Reply("The summary.")]))
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _env(work_dir, fake)) as client:
        run = (await _gql(client, TRIGGER, id=_gid("Automation", "auto-1")))["triggerAutomation"]
        assert (await _finished(database, run))["status"] == "done"
    answer = _tool_results(_main(fake.requests)[1])[-1]
    assert "(writer) — done: Summarise the thing\nThe summary." in answer
    assert row(database, "automation_runs", run)["status"] == "done"
    assert _rows(database, "SELECT id FROM conversations WHERE surface = 'subagent'") == []
    assert _rows(database, "SELECT id FROM thread_messages WHERE thread_id NOT LIKE 'automation%'") == []


async def test_a_wait_on_sub_agents_is_picked_up_after_a_restart(database, work_dir: Path, edge_binary: Path,
                                                                  fake: FakeOllama):
    """The server died while a turn waited on `run_agents`. The sub-agent's
    turn finished on its own; the turn, claimed again, answers the call with
    what it said instead of starting it a second time."""
    _subagent(database, "writer")  # with its parent, and the fake model
    turn, prompt_id = "turn-1", str(uuid.uuid5(uuid.NAMESPACE_URL, "jarvis-turn:turn-1"))
    insert(database, "messages", id="u1", conversation_id="parent", role="user", content="delegate",
           created_at=datetime(2026, 11, 1, 10, 0, 0))
    insert(database, "messages", id=turn, conversation_id="parent", role="assistant", content="", status="running",
           created_at=datetime(2026, 11, 1, 10, 0, 0))
    call = {"id": "call-1", "name": "run_agents", "args": {"tasks": [{"task": "draft the summary", "role": "writer"}]}}
    for seq, (mid, data) in enumerate([
        (prompt_id, {"v": 1, "role": "user", "content": "delegate", "id": prompt_id}),
        ("ai-1", {"v": 1, "role": "assistant", "content": "", "id": "ai-1", "tool_calls": [call]}),
    ]):
        insert(database, "thread_messages", thread_id="parent", seq=seq, message_id=mid, role=data["role"],
               data=json.dumps(data), created_at=datetime(2026, 11, 1, 10, 0, 1))
    insert(database, "jobs", id=turn, kind="chat", status="running", locked_by="edge-1", thread_id="parent", attempts=1,
           locked_until=datetime.now(timezone.utc) + timedelta(minutes=5), created_at=datetime(2026, 11, 1, 10, 0, 0),
           payload=json.dumps({"query": "delegate", "model": MODEL, "conv_id": "parent"}))
    # The sub-agent's turn, started by that call and finished since.
    execute(database, "UPDATE conversations SET spawned_by = ? WHERE id = 'child'", turn)
    insert(database, "messages", id="sub-1", conversation_id="child", role="assistant", content="The summary.",
           status="done", created_at=datetime(2026, 11, 1, 10, 0, 2))
    insert(database, "jobs", id="sub-1", kind="subagent", status="done", thread_id="child", attempts=1,
           created_at=datetime(2026, 11, 1, 10, 0, 2),
           payload=json.dumps({"query": "draft the summary", "model": MODEL, "conv_id": "child", "role": "writer",
                               "by": "agent", "deliver": False}))

    fake.reset([Reply("All done.")])
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", _env(work_dir, fake)):
        job = await _finished(database, turn)
    assert (job["attempts"], job["status"]) == (2, "done")
    [request] = fake.requests
    [answer] = _tool_results(request)
    assert answer.startswith("(The server restarted while this call was waiting.")
    assert answer.endswith("Agent child (writer) — done: draft the summary\nThe summary.")
    assert row(database, "messages", turn)["content"] == "All done."
    assert len(_rows(database, "SELECT id FROM jobs WHERE kind = 'subagent'")) == 1
