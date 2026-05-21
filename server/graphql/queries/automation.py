"""Automation queries — list, single, runs."""

from __future__ import annotations

import strawberry
from strawberry import relay

from db.ops import get_automation, list_automation_runs, list_automations_with_stats

from ..types.automation import Automation, AutomationRun


@strawberry.type
class AutomationQuery:
    @strawberry.field
    async def automations(self, info: strawberry.Info) -> list[Automation]:
        session = info.context["session"]
        rows = await list_automations_with_stats(session)
        return [Automation.from_db(a, stats) for a, stats in rows]

    @strawberry.field
    async def automation(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> Automation | None:
        session = info.context["session"]
        row = await get_automation(session, id.node_id)
        if row is None:
            return None
        return Automation.from_db(row)

    @strawberry.field
    async def automation_runs(
        self,
        info: strawberry.Info,
        automation_id: relay.GlobalID,
    ) -> list[AutomationRun]:
        session = info.context["session"]
        rows = await list_automation_runs(session, automation_id.node_id)
        return [AutomationRun.from_db(r) for r in rows]
