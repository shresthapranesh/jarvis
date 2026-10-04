"""The agent a task-board run gets."""

from __future__ import annotations

from test_agent_golden import GOOGLE, script  # noqa: F401 — the fixture


def test_a_board_run_binds_its_lifecycle_tools(script):  # noqa: F811
    """complete_task/block_task are bound only in a board run — and must be
    tools, or building that agent fails and so does every board run."""
    from core.agents import build_agent

    board = build_agent(GOOGLE, board=True)
    assert {"complete_task", "block_task"} <= set(board.tools_by_name)
    assert not {"complete_task", "block_task"} & set(build_agent(GOOGLE).tools_by_name)
