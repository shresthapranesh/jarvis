"""Durable approvals: the record, the gate, and what a restart is allowed to keep.

The regression this file protects is the split between two kinds of
durability. Persisting the *record* is easy and unconditional. Persisting the
*pause* is not, and it differs per source — a chat interrupt is checkpointed by
LangGraph and can be resumed, a workflow run holds its entire state in memory
and cannot. Getting that wrong in the generous direction is the bad failure:
the inbox shows a button that resumes nothing.
"""

from __future__ import annotations

import asyncio
import json

import pytest


def _context(session, caller: str = "human", conversation_id: str | None = None):
    from server.graphql.extensions import SESSION_LOCK_KEY

    return {
        "session": session,
        SESSION_LOCK_KEY: asyncio.Lock(),
        "caller": caller,
        "caller_conversation_id": conversation_id,
    }


QUERY = """{
  pendingApprovals {
    id source kind question label tool argsJson deferred parentId boardTaskId
  }
}"""


async def _run(query: str = QUERY, *, caller: str = "human", variables=None):
    from db import async_session
    from server.graphql.schema import schema

    async with async_session() as s:
        result = await schema.execute(
            query, context_value=_context(s, caller), variable_values=variables,
        )
    return result


async def _pending():
    result = await _run()
    assert not result.errors, result.errors
    assert result.data is not None
    return result.data["pendingApprovals"]


@pytest.fixture(autouse=True)
def clean_registry():
    from core.state import _tasks

    _tasks.clear()
    yield
    _tasks.clear()


# ── The record ───────────────────────────────────────────────────────────────

async def test_empty_when_nothing_is_waiting(database):
    assert await _pending() == []


async def test_chat_interrupt_is_persisted_with_its_payload(database):
    """The row, not the TaskState, is what the inbox reads — so the payload has
    to survive the write, not just the process."""
    from core.state import TaskState
    from core.streaming import TokenCoalescer, _process_chunk

    state = TaskState(kind="chat", label="Delete the staging DB", parent_id="conv-1")

    class _Interrupt:
        value = {
            "type": "approval",
            "tool": "delete_workflow",
            "args": {"workflow_id": "wf-9"},
            "reason": "Delete workflow wf-9?",
        }
        id = "int-1"

    chunk = ((), "updates", {"__interrupt__": [_Interrupt()]})
    assert await _process_chunk(chunk, state, TokenCoalescer(state), [], task_id="task-1") is True

    rows = await _pending()
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "chat"
    assert row["kind"] == "approval"
    assert row["question"] == "Delete workflow wf-9?"
    assert row["tool"] == "delete_workflow"
    assert json.loads(row["argsJson"]) == {"workflow_id": "wf-9"}
    assert row["deferred"] is False
    # The in-memory copy carries the row id, so the run can close its own row.
    assert state.pending_interrupt is not None
    assert state.pending_interrupt.approval_id == row["id"]


async def test_finished_run_expires_its_open_approvals(database):
    """A run that ends while still waiting leaves an unanswerable request. It
    must not stay listed — that is precisely the dead button `expired` exists
    to prevent."""
    from db import async_session, ops
    from core.streaming import _finalize_message

    async with async_session() as session:
        conv = await ops.get_or_create_conversation(session, "conv-1", model="test:model", title="t")
        msg = await ops.add_message(session, conv.id, "assistant", "")
        await ops.create_approval(
            session, source="chat", kind="approval", question="q?",
            label="run", task_id=msg.id,
        )
    assert len(await _pending()) == 1

    await _finalize_message(msg.id, "done", "done")

    assert await _pending() == []
    async with async_session() as session:
        rows = await ops.list_approvals(session, status=None)
    assert [r.status for r in rows] == ["expired"]


# ── The gate ─────────────────────────────────────────────────────────────────

DELETE_WORKFLOW = """
mutation($id: ID!) { deleteWorkflow(id: $id) }
"""


async def _make_workflow(name: str = "nightly"):
    from db import async_session, ops
    from server.graphql.types.workflow import Workflow as WorkflowType

    async with async_session() as session:
        wf = await ops.create_workflow(session, name=name, description=None, definition="{}")
    from strawberry.relay import to_base64

    return wf, to_base64(WorkflowType.__name__, wf.id)


async def _enable_gating(actions: str = "all"):
    from db import async_session, ops

    async with async_session() as session:
        await ops.set_setting(session, "approval.required_actions", actions)


async def test_gating_is_off_by_default(database):
    """The framework is wired but opts out until an operator turns it on:
    enabling it changes what the agent may do unsupervised, which is an
    install-level decision rather than a default."""
    from db import async_session, ops

    wf, gid = await _make_workflow()
    result = await _run(DELETE_WORKFLOW, caller="agent", variables={"id": gid})

    assert not result.errors, result.errors
    async with async_session() as session:
        assert await ops.get_workflow(session, wf.id) is None
    assert await _pending() == []


async def test_gating_accepts_a_named_subset(database):
    """`all` is not the only setting — an operator can gate one action."""
    await _enable_gating("delete_skill")

    _, gid = await _make_workflow()
    assert not (await _run(DELETE_WORKFLOW, caller="agent", variables={"id": gid})).errors
    assert await _pending() == []


async def test_human_delete_is_not_gated(database):
    """The person clicking Delete in the UI *is* the approval. Making them
    approve themselves would be absurd."""
    from db import async_session, ops

    wf, gid = await _make_workflow()
    result = await _run(DELETE_WORKFLOW, variables={"id": gid})

    assert not result.errors, result.errors
    async with async_session() as session:
        assert await ops.get_workflow(session, wf.id) is None
    assert await _pending() == []


async def test_agent_delete_is_recorded_instead_of_performed(database):
    """The whole point: the agent's destructive write does not happen, and what
    it gets back tells it why."""
    await _enable_gating()
    from db import async_session, ops

    wf, gid = await _make_workflow()
    result = await _run(DELETE_WORKFLOW, caller="agent", variables={"id": gid})

    assert result.errors
    assert "Approval required" in str(result.errors[0].message)
    async with async_session() as session:
        assert await ops.get_workflow(session, wf.id) is not None  # untouched

    rows = await _pending()
    assert len(rows) == 1
    assert rows[0]["deferred"] is True
    assert rows[0]["tool"] == "delete_workflow"
    assert "nightly" in rows[0]["question"]


async def test_repeated_agent_attempts_collapse_to_one_request(database):
    """An agent that retries in a loop must not fill the inbox with copies."""
    await _enable_gating()
    _, gid = await _make_workflow()
    for _ in range(3):
        result = await _run(DELETE_WORKFLOW, caller="agent", variables={"id": gid})
        assert result.errors

    assert len(await _pending()) == 1


async def test_gating_is_configurable(database):
    from db import async_session, ops

    async with async_session() as session:
        await ops.set_setting(session, "approval.required_actions", "none")

    wf, gid = await _make_workflow()
    result = await _run(DELETE_WORKFLOW, caller="agent", variables={"id": gid})

    assert not result.errors, result.errors
    async with async_session() as session:
        assert await ops.get_workflow(session, wf.id) is None


# ── Resolution ───────────────────────────────────────────────────────────────

RESOLVE = """
mutation($id: String!, $answer: String!) {
  resolveApproval(id: $id, answer: $answer) { id status result }
}
"""


async def test_approving_a_deferred_action_executes_it(database):
    await _enable_gating()
    from db import async_session, ops

    wf, gid = await _make_workflow()
    await _run(DELETE_WORKFLOW, caller="agent", variables={"id": gid})
    approval_id = (await _pending())[0]["id"]

    result = await _run(RESOLVE, variables={"id": approval_id, "answer": "approve"})
    assert not result.errors, result.errors
    assert result.data is not None
    assert result.data["resolveApproval"]["status"] == "approved"

    async with async_session() as session:
        assert await ops.get_workflow(session, wf.id) is None
    assert await _pending() == []


async def test_denying_a_deferred_action_leaves_the_target_alone(database):
    await _enable_gating()
    from db import async_session, ops

    wf, gid = await _make_workflow()
    await _run(DELETE_WORKFLOW, caller="agent", variables={"id": gid})
    approval_id = (await _pending())[0]["id"]

    result = await _run(RESOLVE, variables={"id": approval_id, "answer": "deny"})
    assert not result.errors, result.errors
    assert result.data is not None
    assert result.data["resolveApproval"]["status"] == "denied"

    async with async_session() as session:
        assert await ops.get_workflow(session, wf.id) is not None


async def test_ambiguous_answer_denies(database):
    """A reply matching no approve/deny keyword is usually a question. Running
    a destructive action on that basis is the wrong default."""
    await _enable_gating()
    wf, gid = await _make_workflow()
    await _run(DELETE_WORKFLOW, caller="agent", variables={"id": gid})
    approval_id = (await _pending())[0]["id"]

    result = await _run(RESOLVE, variables={"id": approval_id, "answer": "why do you want to?"})
    assert result.data is not None
    assert result.data["resolveApproval"]["status"] == "denied"

    from db import async_session, ops
    async with async_session() as session:
        assert await ops.get_workflow(session, wf.id) is not None


async def test_double_resolve_is_refused(database):
    """Two tabs, or a retried mutation, must not run the delete twice."""
    await _enable_gating()
    _, gid = await _make_workflow()
    await _run(DELETE_WORKFLOW, caller="agent", variables={"id": gid})
    approval_id = (await _pending())[0]["id"]

    assert not (await _run(RESOLVE, variables={"id": approval_id, "answer": "approve"})).errors
    second = await _run(RESOLVE, variables={"id": approval_id, "answer": "approve"})
    assert second.errors
    assert "already approved" in str(second.errors[0].message)


async def test_resolving_a_blocking_row_whose_run_is_gone_reports_it(database):
    """Better a visible error than a success message for an answer nobody got."""
    from db import async_session, ops

    async with async_session() as session:
        row = await ops.create_approval(
            session, source="chat", kind="input", question="which one?",
            label="run", task_id="task-gone",
        )

    result = await _run(RESOLVE, variables={"id": row.id, "answer": "the first"})
    assert result.errors
    assert "no longer waiting" in str(result.errors[0].message)

    async with async_session() as session:
        row_now = await ops.get_approval(session, row.id)
        assert row_now is not None and row_now.status == "expired"


# ── Board ────────────────────────────────────────────────────────────────────

async def test_answering_a_board_task_records_an_answer_not_a_cancellation(database):
    """`update_board_task` closes open approvals as `cancelled` whenever a task
    stops waiting — so an answer resolved in the wrong order gets filed as a
    cancellation. Order is load-bearing here."""
    from db import async_session, ops

    async with async_session() as session:
        task = await ops.create_board_task(session, title="Migrate the index")
        await ops.update_board_task(
            session, task.id, status="blocked", blocked_kind="needs_input",
            blocked_reason="Which index first?",
        )
        await ops.create_approval(
            session, source="board_task", kind="input",
            question="Which index first?", label=task.title, board_task_id=task.id,
        )
    approval_id = (await _pending())[0]["id"]

    result = await _run(RESOLVE, variables={"id": approval_id, "answer": "the docs one"})
    assert not result.errors, result.errors
    assert result.data is not None
    assert result.data["resolveApproval"]["status"] == "answered"

    async with async_session() as session:
        refreshed = await ops.get_board_task(session, task.id)
        assert refreshed is not None
        assert refreshed.status == "ready"
        assert refreshed.pending_answer == "the docs one"
        closed = await ops.get_approval(session, approval_id)
        assert closed is not None and closed.answer == "the docs one"


async def test_board_task_moving_off_needs_input_clears_its_approval(database):
    """Stopping or re-queuing a task from the board must not leave its question
    in the inbox."""
    from db import async_session, ops

    async with async_session() as session:
        task = await ops.create_board_task(session, title="t")
        await ops.update_board_task(
            session, task.id, status="blocked", blocked_kind="needs_input",
            blocked_reason="?",
        )
        await ops.create_approval(
            session, source="board_task", kind="input", question="?",
            label="t", board_task_id=task.id,
        )
        assert len(await _pending()) == 1
        await ops.update_board_task(session, task.id, status="archived", blocked_kind=None)

    assert await _pending() == []


# ── Restart reconciliation ───────────────────────────────────────────────────

async def test_workflow_pause_expires_on_restart(database):
    """The engine holds its whole run state in memory and checkpoints nothing,
    so a restart re-runs the graph and asks again. The old row is dead."""
    from core.approvals import reconcile_startup
    from db import async_session, ops

    async with async_session() as session:
        row = await ops.create_approval(
            session, source="workflow", kind="approval", question="Ship it?",
            label="deploy", task_id="run-1",
        )

    await reconcile_startup()

    async with async_session() as session:
        row_now = await ops.get_approval(session, row.id)
        assert row_now is not None and row_now.status == "expired"


async def test_chat_pause_survives_restart_while_its_job_does(database):
    """LangGraph checkpointed the interrupt, so the run is resumable — but only
    while a job row still exists to re-claim it."""
    from core.approvals import reconcile_startup
    from db import async_session, ops
    from db.models import Job

    async with async_session() as session:
        session.add(Job(id="task-live", kind="chat", payload="{}", status="pending"))
        await session.commit()
        live = await ops.create_approval(
            session, source="chat", kind="approval", question="ok?",
            label="chat", task_id="task-live",
        )
        orphan = await ops.create_approval(
            session, source="chat", kind="approval", question="ok?",
            label="chat", task_id="task-dead",
        )

    await reconcile_startup()

    async with async_session() as session:
        live_now = await ops.get_approval(session, live.id)
        orphan_now = await ops.get_approval(session, orphan.id)
        assert live_now is not None and live_now.status == "pending"
        assert orphan_now is not None and orphan_now.status == "expired"


async def test_deferred_requests_survive_restart_unconditionally(database):
    """Nothing was waiting on them, so a restart destroyed nothing."""
    from core.approvals import reconcile_startup
    from db import async_session, ops

    async with async_session() as session:
        row = await ops.create_approval(
            session, source="chat", kind="approval", question="delete?",
            label="Delete workflow", action="delete_workflow",
            action_payload=json.dumps({"workflow_id": "wf-1"}),
        )

    await reconcile_startup()

    async with async_session() as session:
        row_now = await ops.get_approval(session, row.id)
        assert row_now is not None and row_now.status == "pending"


async def test_restart_backfills_board_tasks_blocked_before_the_table_existed(database):
    from core.approvals import reconcile_startup
    from db import async_session, ops

    async with async_session() as session:
        task = await ops.create_board_task(session, title="older question")
        await ops.update_board_task(
            session, task.id, status="blocked", blocked_kind="needs_input",
            blocked_reason="still waiting",
        )
    assert await _pending() == []

    await reconcile_startup()

    rows = await _pending()
    assert len(rows) == 1
    assert rows[0]["boardTaskId"] == task.id
    assert rows[0]["question"] == "still waiting"


async def test_reconcile_is_idempotent(database):
    """It runs on every boot; a second pass must not duplicate the backfill."""
    from core.approvals import reconcile_startup
    from db import async_session, ops

    async with async_session() as session:
        task = await ops.create_board_task(session, title="q")
        await ops.update_board_task(
            session, task.id, status="blocked", blocked_kind="needs_input",
            blocked_reason="?",
        )

    await reconcile_startup()
    await reconcile_startup()

    assert len(await _pending()) == 1
