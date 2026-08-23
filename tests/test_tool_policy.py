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

import asyncio

import pytest
from langgraph.graph import MessagesState


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

async def test_resolving_the_row_releases_the_waiter(database):
    from core.approvals import resolve
    from core.tool_gate import create_gate_request, wait_for_gate
    from db import async_session

    async with async_session() as session:
        row = await create_gate_request(
            session, tool_key="bound:run_cell", tool_name="run_cell",
            args={"code": "print(1)"}, conversation_id="conv-1",
        )
    approval_id = row.id

    waiter = asyncio.create_task(wait_for_gate(approval_id, timeout=10))
    await asyncio.sleep(0)  # let the waiter register before the answer lands

    async with async_session() as session:
        resolved = await resolve(session, approval_id, "approve")
    assert resolved.status == "approved"

    approved, answer = await asyncio.wait_for(waiter, timeout=5)
    assert approved is True
    assert answer == "approve"


async def test_denial_is_denial_not_ambiguity(database):
    """`is_affirmative_answer` denies anything it cannot read as a yes, and the
    gate must inherit that: running a gated tool on an unparseable reply is the
    wrong default."""
    from core.approvals import resolve
    from core.tool_gate import create_gate_request, wait_for_gate
    from db import async_session

    async with async_session() as session:
        row = await create_gate_request(
            session, tool_key="sdk:delete_skill", tool_name="jarvis.delete_skill",
            args={"skill_id": "s-1"}, conversation_id="conv-1",
        )

    waiter = asyncio.create_task(wait_for_gate(row.id, timeout=10))
    await asyncio.sleep(0)
    async with async_session() as session:
        await resolve(session, row.id, "what does it do?")

    approved, _ = await asyncio.wait_for(waiter, timeout=5)
    assert approved is False


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


async def test_open_gate_holds_the_kernel_cell(database):
    """`run_cell`'s 60s timeout is suspended only while a request is actually
    open — otherwise a genuinely hung cell would never be interrupted."""
    from core.approvals import resolve
    from core.tool_gate import create_gate_request, has_open_gate
    from db import async_session

    assert await has_open_gate("conv-1") is False
    async with async_session() as session:
        row = await create_gate_request(
            session, tool_key="sdk:create_task", tool_name="jarvis.create_task",
            args={}, conversation_id="conv-1",
        )
    assert await has_open_gate("conv-1") is True
    assert await has_open_gate("conv-2") is False

    async with async_session() as session:
        await resolve(session, row.id, "deny")
    assert await has_open_gate("conv-1") is False


# ── The gated tool node ──────────────────────────────────────────────────────

async def _ai_message(calls):
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=calls)


async def test_denied_call_is_answered_and_never_executed(database):
    from langchain_core.messages import ToolMessage
    from langchain_core.tools import tool

    from core.approvals import resolve
    from core.tool_gate_node import make_gated_tool_node
    from core.tool_policy import set_tool_policy
    from db import async_session

    ran: list[str] = []

    @tool
    async def dangerous(target: str) -> str:
        """Do something that wants a human's say-so first."""
        ran.append(target)
        return "done"

    async with async_session() as session:
        await set_tool_policy(session, "bound:dangerous", approval=True)

    node = make_gated_tool_node([dangerous])
    call = {"name": "dangerous", "args": {"target": "prod"}, "id": "call-1", "type": "tool_call"}
    state = {"messages": [await _ai_message([call])]}

    task = asyncio.create_task(node(state))
    # The row appears once the node blocks; answer it the way a human would.
    row = None
    for _ in range(50):
        await asyncio.sleep(0.05)
        async with async_session() as session:
            from db import ops

            rows = await ops.list_approvals(session)
            if rows:
                row = rows[0]
                break
    assert row is not None, "the gated call should have recorded a request"
    async with async_session() as session:
        await resolve(session, row.id, "deny")

    result = await asyncio.wait_for(task, timeout=10)
    assert ran == [], "a denied tool must not run"
    messages = result["messages"] if isinstance(result, dict) else []
    assert len(messages) == 1
    answer = messages[0]
    assert isinstance(answer, ToolMessage)
    # The pairing matters more than the text: an unanswered tool_use is what
    # breaks the *next* provider call.
    assert answer.tool_call_id == "call-1"
    assert "Denied by a human" in answer.content


async def test_ungated_calls_pass_straight_through(database):
    """Driven through a compiled graph, not by calling the node directly:
    `ToolNode` resolves injected arguments from the LangGraph runtime, so a
    bare call would test a harness that does not exist in production."""
    from langchain_core.tools import tool
    from langgraph.graph import END, START, StateGraph

    from core.tool_gate_node import make_gated_tool_node

    @tool
    async def harmless(value: str) -> str:
        """Nothing to approve here."""
        return f"got {value}"

    call = {"name": "harmless", "args": {"value": "x"}, "id": "call-2", "type": "tool_call"}

    async def model(_state: MessagesState) -> dict:
        return {"messages": [await _ai_message([call])]}

    # Same suppressions core/agents.py carries: pyrefly does not model
    # langgraph's StateT bound or its node-callable union.
    graph = StateGraph(MessagesState)  # type: ignore[type-var]
    graph.add_node("model", model)  # type: ignore[bad-argument-type]
    graph.add_node("tools", make_gated_tool_node([harmless]))  # type: ignore[bad-argument-type]
    graph.add_edge(START, "model")
    graph.add_edge("model", "tools")
    graph.add_edge("tools", END)

    out = await graph.compile().ainvoke({"messages": []})
    assert out["messages"][-1].content == "got x"
