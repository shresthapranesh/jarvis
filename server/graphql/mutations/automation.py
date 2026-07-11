"""Automation mutations — create, update, delete, trigger, stop run."""

from __future__ import annotations

import strawberry
from strawberry import relay

from apscheduler.triggers.cron import CronTrigger

from core.agents import is_valid_model
from core.state import _tasks, get_queue
from core.scheduler import _register_scheduler_job, _remove_scheduler_job
from db.ops import (
    create_automation as db_create_automation,
    delete_automation as db_delete_automation,
    update_automation as db_update_automation,
)

from ..types.automation import Automation
from server.automation_runtime import register_automation_run


@strawberry.input
class AutomationInput:
    name: str
    input_type: str  # "prompt" | "code" | "webhook" | "monitor"
    description: str | None = None
    prompt_text: str | None = None
    model: str | None = None
    code_text: str | None = None
    webhook_url: str | None = None
    webhook_method: str | None = None
    webhook_headers: str | None = None  # JSON string
    webhook_body: str | None = None
    schedule: str | None = None  # cron expression
    enabled: bool = True
    stateful: bool = False  # prompt type only: share one thread across runs
    notifications: str | None = None  # JSON string


def _validate_input(input: AutomationInput) -> None:
    if input.model is not None and not is_valid_model(input.model):
        raise ValueError(f"unknown model {input.model!r}; query `models` for the catalog")
    if input.schedule:
        try:
            CronTrigger.from_crontab(input.schedule)
        except Exception:
            raise ValueError("invalid cron expression")


@strawberry.type
class AutomationMutation:
    @strawberry.mutation
    async def create_automation(
        self,
        info: strawberry.Info,
        input: AutomationInput,
    ) -> Automation:
        _validate_input(input)
        session = info.context["session"]
        auto = await db_create_automation(
            session,
            name=input.name,
            description=input.description,
            input_type=input.input_type,
            prompt_text=input.prompt_text,
            model=input.model,
            code_text=input.code_text,
            webhook_url=input.webhook_url,
            webhook_method=input.webhook_method,
            webhook_headers=input.webhook_headers,
            webhook_body=input.webhook_body,
            schedule=input.schedule,
            enabled=input.enabled,
            stateful=input.stateful,
            notifications=input.notifications,
        )
        if auto.enabled and auto.schedule:
            _register_scheduler_job(auto)
        return Automation.from_db(auto)

    @strawberry.mutation
    async def update_automation(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
        input: AutomationInput,
    ) -> Automation:
        _validate_input(input)
        session = info.context["session"]
        auto = await db_update_automation(
            session,
            id.node_id,
            name=input.name,
            description=input.description,
            input_type=input.input_type,
            prompt_text=input.prompt_text,
            model=input.model,
            code_text=input.code_text,
            webhook_url=input.webhook_url,
            webhook_method=input.webhook_method,
            webhook_headers=input.webhook_headers,
            webhook_body=input.webhook_body,
            schedule=input.schedule,
            enabled=input.enabled,
            stateful=input.stateful,
            notifications=input.notifications,
        )
        if auto is None:
            raise ValueError("automation not found")
        _remove_scheduler_job(id.node_id)
        if auto.enabled and auto.schedule:
            _register_scheduler_job(auto)
        return Automation.from_db(auto)

    @strawberry.mutation
    async def delete_automation(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> bool:
        _remove_scheduler_job(id.node_id)
        session = info.context["session"]
        deleted = await db_delete_automation(session, id.node_id)
        if not deleted:
            raise ValueError("automation not found")
        return True

    @strawberry.mutation
    async def trigger_automation(
        self,
        info: strawberry.Info,
        id: relay.GlobalID,
    ) -> str:
        """Returns the run_id; client subscribes to automationRunEvents(runId)."""
        session = info.context["session"]
        run_id = await register_automation_run(session, id.node_id)
        if run_id is None:
            raise ValueError("automation not found")
        return run_id

    @strawberry.mutation
    async def stop_automation_run(self, run_id: str) -> bool:
        state = _tasks.get(run_id)
        if state is None:
            raise ValueError("run not found or already finished")
        if state.done:
            raise ValueError("run already finished")
        # In-process fast path so the running handler observes immediately
        # (no waiting for the cancel watcher's next poll tick).
        state.cancelled = True
        state._stop_event.set()
        # Durable + cross-process path: queue-side cancel transitions a
        # not-yet-claimed pending job to 'cancelled' and sets cancel_requested
        # for a running job. job.id == run_id by convention.
        await get_queue().cancel(run_id)
        return True
