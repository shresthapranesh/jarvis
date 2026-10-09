"""Workflow runs in the edge (`edge/src/agent/workflow/`), against a scripted
model behind the fake Ollama server: a branching graph of agent and
conditional nodes, a run paused on a human, and `run_workflow` called from a
chat turn without the turn going to Python.

Edge-only — not diffed against Python's engine.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from edge_support import _gid, _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture
from test_edge_loop import MODEL, START, FakeOllama, Reply, _rows, fake  # noqa: F401 — fake is a fixture
from seed import insert
from test_edge_runs import WORKFLOW

REPO = Path(__file__).resolve().parent.parent
RUN = "mutation($id: ID!, $inputs: JSON) { runWorkflow(id: $id, inputs: $inputs) }"


@pytest.fixture
async def edge(database, work_dir: Path, fake: FakeOllama, edge_binary: Path):
    """An edge running the agent loop over a fresh database whose default
    model is the fake."""
    insert(database, "config_settings", key="models.custom", value=json.dumps([{"id": MODEL, "label": "Fake"}]))
    insert(database, "config_settings", key="default.model", value=MODEL)
    env = {"JARVIS_RUN_JOBS": "1", "OLLAMA_HOST": fake.url, "HOME": str(work_dir), "JARVIS_APP_DIR": str(REPO),
           "JARVIS_BROWSER_CDP_URL": "http://127.0.0.1:1"}
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env) as client:
        yield client


async def _workflow(work_dir: Path, graph: dict[str, Any]) -> str:
    return insert(work_dir / "database.db", "workflows", id=str(uuid4()), name="Flow", definition=json.dumps(graph))["id"]


async def _gql(client, query: str, variables: dict) -> dict:
    body = (await client.post("/graphql", json={"query": query, "variables": variables})).json()
    assert "errors" not in body, body
    return body["data"]


async def _subscribe(client, run_id: str) -> list[dict]:
    import websockets

    port = client.base_url.port
    async with websockets.connect(f"ws://127.0.0.1:{port}/graphql", subprotocols=["graphql-transport-ws"]) as ws:
        await ws.send(json.dumps({"type": "connection_init", "payload": {}}))
        assert json.loads(await ws.recv())["type"] == "connection_ack"
        await ws.send(json.dumps({"id": "1", "type": "subscribe", "payload": {"query": WORKFLOW, "variables": {"id": run_id}}}))
        out = []
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 60))
            if msg["type"] == "next":
                out.append(msg["payload"]["data"]["workflowRunEvents"])
            elif msg["type"] in ("complete", "error"):
                return out


async def _until(db: Path, sql: str, *args: Any) -> list[tuple]:
    for _ in range(200):
        rows = _rows(db, sql, *args)
        if rows:
            return rows
        await asyncio.sleep(0.05)
    raise AssertionError(f"never: {sql}")


async def test_a_branching_graph_runs_in_the_edge(edge, fake, work_dir):
    wid = await _workflow(work_dir, {
        "nodes": [
            {"id": "s", "type": "start", "config": {"initial_inputs": {"topic": "maps"}}},
            {"id": "a", "type": "agent", "label": "Draft", "config": {"prompt_template": "Summarize {{topic}}",
                                                                      "output_key": "summary"}},
            {"id": "c", "type": "conditional", "config": {"condition": "Good? {{summary}}"}},
            {"id": "t", "type": "agent", "config": {"prompt_template": "Polish {{nodes.a.summary | upper}}"}},
            {"id": "f", "type": "agent", "config": {"prompt_template": "Redo"}},
        ],
        "edges": [
            {"id": "e1", "source": "s", "target": "a", "sourceHandle": "topic"},
            {"id": "e2", "source": "a", "target": "c", "sourceHandle": "summary"},
            {"id": "e3", "source": "c", "target": "t", "sourceHandle": "true"},
            {"id": "e4", "source": "c", "target": "f", "sourceHandle": "false"},
        ],
    })
    fake.reset([Reply("A short summary."), Reply("TRUE."), Reply("Polished.")])
    run_id = (await _gql(edge, RUN, {"id": _gid("Workflow", wid), "inputs": {"topic": "rivers"}}))["runWorkflow"]
    events = await _subscribe(edge, run_id)

    db = work_dir / "database.db"
    [(status, outputs, results, job)] = _rows(
        db, "SELECT r.status, r.outputs, r.node_results, j.status FROM workflow_runs r JOIN jobs j ON j.id = r.id "
            "WHERE r.id = ?", run_id)
    assert (status, job) == ("done", "done")
    assert json.loads(outputs) == {"result": "Polished."}
    records = json.loads(results)
    assert [(r["node_id"], r["status"]) for r in records] == [("s", "done"), ("a", "done"), ("c", "done"), ("t", "done")]
    assert records[1]["rendered_prompt"] == "Summarize rivers"
    assert records[2]["verdict"] == "true"

    # The agent nodes are the main agent: its system prompt, the node's prompt.
    asked = [r["messages"][-1]["content"] for r in fake.requests]
    assert asked == ["Summarize rivers", "Good? A short summary.", "Polish A SHORT SUMMARY."]
    assert fake.requests[0]["messages"][0]["role"] == "system" and fake.requests[0]["tools"]
    assert fake.requests[1]["messages"][0]["content"].startswith("You are a routing assistant.")

    kinds = [e["__typename"] for e in events]
    assert kinds[-1] == "WorkflowDoneEvent" and "WorkflowNodeErrorEvent" not in kinds
    tokens = "".join(e["text"] for e in events if e["__typename"] == "WorkflowNodeTokenEvent" and e["nodeId"] == "a")
    assert tokens == "A short summary."
    assert {"__typename": "WorkflowNodeConditionEvent", "nodeId": "c", "verdict": "true"} in events
    assert not any(e.get("nodeId") == "f" for e in events)


async def test_a_run_paused_on_people_is_answered_through_the_edge(edge, fake, work_dir):
    wid = await _workflow(work_dir, {
        "nodes": [
            {"id": "h", "type": "human_input", "config": {"prompt": "Name?"}},
            {"id": "p", "type": "approval", "config": {"reason": "Ship it for {{answer}}?", "on_deny": "continue"}},
            {"id": "ok", "type": "start", "config": {"initial_inputs": {"went": "ok"}}},
            {"id": "no", "type": "start", "config": {"initial_inputs": {"went": "no"}}},
        ],
        "edges": [
            {"id": "e1", "source": "h", "target": "p", "sourceHandle": "answer"},
            {"id": "e2", "source": "p", "target": "ok", "sourceHandle": "approved"},
            {"id": "e3", "source": "p", "target": "no", "sourceHandle": "denied"},
        ],
    })
    fake.reset([])
    db = work_dir / "database.db"
    run_id = (await _gql(edge, RUN, {"id": _gid("Workflow", wid)}))["runWorkflow"]
    subscriber = asyncio.create_task(_subscribe(edge, run_id))

    await _until(db, "SELECT id FROM approvals WHERE task_id = ? AND kind = 'input' AND status = 'pending'", run_id)
    resumed = await _gql(edge, "mutation($r: String!, $a: String!) { resumeWorkflowRun(runId: $r, answer: $a) }",
                         {"r": run_id, "a": "Ada"})
    assert resumed == {"resumeWorkflowRun": True}
    [(approval, question)] = await _until(
        db, "SELECT id, question FROM approvals WHERE task_id = ? AND kind = 'approval' AND status = 'pending'", run_id)
    assert question == "Ship it for Ada?"
    # From the inbox, as a human would.
    answered = await _gql(edge, "mutation($id: String!) { resolveApproval(id: $id, answer: \"no\") { status result } }",
                          {"id": approval})
    assert answered["resolveApproval"] == {"status": "denied", "result": "Delivered to the run."}
    events = await asyncio.wait_for(subscriber, 60)

    [(status, outputs)] = _rows(db, "SELECT status, outputs FROM workflow_runs WHERE id = ?", run_id)
    assert (status, json.loads(outputs)) == ("done", {"went": "no"})
    rows = _rows(db, "SELECT kind, status, answer, source, interrupt_id FROM approvals WHERE task_id = ? ORDER BY kind", run_id)
    assert rows == [("approval", "denied", "no", "workflow", "p"), ("input", "answered", "Ada", "workflow", "h")]
    kinds = [e["__typename"] for e in events]
    assert kinds.count("WorkflowInterruptEvent") == 2 and kinds.count("WorkflowApprovalRequestEvent") == 2
    assert {"__typename": "WorkflowApprovalResolvedEvent", "tool": "p", "approved": False, "answer": "no",
            "nodeId": "p"} in events
    assert not fake.requests


async def test_run_workflow_from_a_chat_turn(edge, fake, work_dir):
    wid = await _workflow(work_dir, {"nodes": [{"id": "n", "type": "agent", "config": {"prompt_template": "Say {{topic}}"}}],
                           "edges": []})
    fake.reset([
        Reply(calls=[("run_workflow", {"workflow_id": wid, "inputs_json": json.dumps({"topic": "hi"})})]),
        Reply("Inner."),
        Reply("Done."),
    ])
    started = (await _gql(edge, START, {"input": {"query": "Run the flow", "model": MODEL}}))["startTask"]
    db = work_dir / "database.db"
    await _until(db, "SELECT 1 FROM jobs WHERE id = ? AND status = 'done'", started["taskId"])

    [(content,)] = _rows(db, "SELECT content FROM messages WHERE id = ?", started["taskId"])
    assert content == "Done."
    assert fake.requests[1]["messages"][-1]["content"] == "Say hi"
    result = next(m for m in fake.requests[2]["messages"] if m["role"] == "tool")
    assert json.loads(result["content"]) == {"result": "Inner."}
    steps = [node for (node,) in _rows(db, "SELECT node FROM steps WHERE message_id = ? ORDER BY seq", started["taskId"])]
    assert "worker_start" in steps and "worker_done" in steps
