"""The kernel's way of saying "I am in the browser right now".

`read(url, browser=True)` runs in a kernel process, which has no LangGraph
runtime — so `adispatch_custom_event`, the way every other tool emits a live
event, is unavailable there. This is the same relay `requestToolApproval` is
for approvals: an ordinary mutation the kernel calls over HTTP, which then
writes onto the live run's `TaskState` from inside the server where the event
stream lives.

Without it a browse is invisible: the activity sidebar shows `run_cell` and
nothing tells the UI a browser is worth watching.
"""

from __future__ import annotations

import strawberry

from core.tool_gate import _emit_to_run, live_task_id

_PHASES = ("start", "done", "error")


@strawberry.type
class BrowserMutation:
    @strawberry.mutation
    async def browser_activity(
        self,
        info: strawberry.Info,
        url: str,
        phase: str = "start",
        conversation_id: str | None = None,
    ) -> bool:
        """Announce a browser navigation on the conversation's live run.

        Returns whether it reached a run — false is normal, not an error: a CLI
        or bot browse has no live `TaskState` to announce onto, and the caller
        must not treat that as a failure worth retrying.
        """
        if info.context.get("caller") != "agent":
            raise ValueError("browserActivity is only for agent-initiated calls")
        if phase not in _PHASES:
            raise ValueError(f"phase must be one of: {', '.join(_PHASES)}")

        conv = conversation_id or info.context.get("caller_conversation_id")
        task_id = live_task_id(conv)
        if not task_id:
            return False
        _emit_to_run(task_id, "browser_step", url=url[:500], phase=phase, source="main")
        return True
