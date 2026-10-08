"""Per-tool policy: the inventory, the switch, and the blocking gate.

Three properties are worth protecting here, because each one is easy to get
subtly wrong and impossible to notice from the UI:

1. **Disabled means unbound.** A tool that is merely refused at call time still
   costs its schema on every LLM call and still gets attempted; the filter has
   to run at graph-build time.
2. **The gate actually blocks, and denial answers the tool call.** A denied
   call must come back as a `ToolMessage` for that exact `tool_call_id` —
   leaving a `tool_use` without its `tool_result` is what Anthropic/Bedrock
   reject on the *next* call, i.e. far from the cause.
3. **Resolution reaches the waiter.** The row is the rendezvous, so closing it
   is what releases a blocked caller — in this process or in a kernel.
"""

from __future__ import annotations


import pytest


@pytest.fixture(autouse=True)
def clear_policy_cache():
    from core import tool_policy

    tool_policy.invalidate_cache(None)
    yield
    tool_policy.invalidate_cache(None)


# ── Storage ──────────────────────────────────────────────────────────────────

async def test_defaults_are_permissive_and_store_nothing(database):
    from core.tool_policy import CONFIG_KEY, needs_approval, is_enabled
    from db import async_session, ops

    assert is_enabled("bound:run_cell") is True
    assert needs_approval("bound:run_cell") is False
    async with async_session() as session:
        assert await ops.get_setting(session, CONFIG_KEY) is None


async def test_set_and_clear_round_trips(database):
    from core.tool_policy import CONFIG_KEY, is_enabled, needs_approval, set_tool_policy
    from db import async_session, ops

    async with async_session() as session:
        await set_tool_policy(session, "sdk:delete_workflow", approval=True)
    assert needs_approval("sdk:delete_workflow") is True
    assert is_enabled("sdk:delete_workflow") is True

    async with async_session() as session:
        await set_tool_policy(session, "sdk:delete_workflow", enabled=False)
    # The two switches are independent — setting one must not reset the other.
    assert needs_approval("sdk:delete_workflow") is True
    assert is_enabled("sdk:delete_workflow") is False

    async with async_session() as session:
        await set_tool_policy(session, "sdk:delete_workflow", enabled=True, approval=False)
        # Back to the default: the entry is dropped rather than stored as one
        # that says "default", so the map only ever holds real decisions.
        raw = await ops.get_setting(session, CONFIG_KEY)
    assert raw is not None and "delete_workflow" not in raw


async def test_unknown_key_is_refused(database):
    from core.tool_policy import set_tool_policy
    from db import async_session

    async with async_session() as session:
        with pytest.raises(ValueError):
            await set_tool_policy(session, "nonsense", enabled=False)


# ── Inventory ────────────────────────────────────────────────────────────────

async def test_inventory_spans_bound_and_sdk(database):
    from core.tool_policy import KIND_BOUND, KIND_SDK, tool_inventory

    rows = {t.key: t for t in tool_inventory()}
    assert "bound:run_cell" in rows
    assert rows["bound:run_cell"].kind == KIND_BOUND
    # Bound tools cost tokens on every call; SDK ones do not — the distinction
    # the Tools page exists to make visible.
    assert rows["bound:run_cell"].in_prompt is True

    sdk_rows = [t for t in rows.values() if t.kind == KIND_SDK]
    assert sdk_rows, "the jarvis SDK should contribute to the inventory"
    assert all(t.in_prompt is False for t in sdk_rows)
    assert "sdk:create_automation" in rows
    assert rows["sdk:create_automation"].description


async def test_inventory_reflects_policy(database):
    from core.tool_policy import set_tool_policy, tool_inventory
    from db import async_session

    async with async_session() as session:
        await set_tool_policy(session, "bound:write_artifact", enabled=False, approval=True)
    row = next(t for t in tool_inventory() if t.key == "bound:write_artifact")
    assert row.enabled is False
    assert row.requires_approval is True


# ── Binding ──────────────────────────────────────────────────────────────────

async def test_disabled_tool_is_not_bound(database):
    """The filter the agent builder applies — a disabled tool never reaches
    `bind_tools`, so the model is not even told it exists."""
    from core.agents import _allowed
    from core.tool_policy import set_tool_policy
    from db import async_session
    from tools.code import run_cell
    from tools.todos import write_todos

    assert [t.name for t in _allowed([run_cell, write_todos])] == ["run_cell", "write_todos"]

    async with async_session() as session:
        await set_tool_policy(session, "bound:write_todos", enabled=False)
    assert [t.name for t in _allowed([run_cell, write_todos])] == ["run_cell"]


# ── The gate ─────────────────────────────────────────────────────────────────

async def test_gate_shows_up_as_blocking_in_the_inbox(database):
    """Not `deferred`: something *is* waiting on this one, and the inbox says
    "runs on approval" only for requests where approving performs the work."""
    from core.tool_gate import create_gate_request
    from db import async_session, ops

    async with async_session() as session:
        await create_gate_request(
            session, tool_key="bound:run_cell", tool_name="run_cell",
            args={"code": "x"}, conversation_id="conv-1",
        )
        rows = await ops.list_approvals(session)
    assert len(rows) == 1
    assert rows[0].action is None          # → deferred=False in the GraphQL type
    assert rows[0].source == "tool"
    assert rows[0].parent_id == "conv-1"


# ── The gate in the agent loop ───────────────────────────────────────────────

def _one_call_agent(tool, call):
    """An agent whose model asks for `call` once, then stops."""
    from langchain_core.messages import AIMessage

    from core.agent_loop import Agent
    from core.tool_gate_node import make_tool_gate

    async def step(run):
        if any(isinstance(m, AIMessage) for m in run.thread.messages):
            return [AIMessage(content="ok")]
        return [AIMessage(content="", tool_calls=[call])]

    return Agent("test", step, [tool], gate=make_tool_gate([tool]))


async def test_ungated_calls_pass_straight_through(database):
    from langchain_core.messages import ToolMessage
    from langchain_core.tools import tool

    from core.agent_loop import Thread

    @tool
    async def harmless(value: str) -> str:
        """Nothing to approve here."""
        return f"got {value}"

    call = {"name": "harmless", "args": {"value": "x"}, "id": "call-2", "type": "tool_call"}
    out = await _one_call_agent(harmless, call).ainvoke({"messages": [("user", "go")]}, thread=Thread())
    results = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert [r.content for r in results] == ["got x"]


# ── Reaching the conversation, not just the inbox ────────────────────────────

async def test_no_live_run_is_not_an_error(database):
    """Board tasks, automations and CLI runs gate the same way; a request with
    no stream to announce on is normal, not a failure."""
    from core.tool_gate import create_gate_request
    from db import async_session

    async with async_session() as session:
        row = await create_gate_request(
            session, tool_key="sdk:delete_skill", tool_name="jarvis.delete_skill",
            args={}, conversation_id="conv-nobody-home",
        )
    assert row.task_id is None
