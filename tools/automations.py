"""Agent tools for managing automations via the Jarvis database."""
from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger

from core.scheduler import _register_scheduler_job, _remove_scheduler_job
from db.engine import async_session
from db.ops import (
    create_automation as _create,
    delete_automation as _delete,
    list_automations as _list,
    update_automation as _update,
)

_INPUT_TYPES = ("prompt", "code", "webhook", "monitor")


def _invalid_input(input_type: str | None, schedule: str | None) -> str | None:
    """Validate the bits that would otherwise fail silently at run/registration
    time; returns an agent-facing error string or None."""
    if input_type is not None and input_type not in _INPUT_TYPES:
        return f"Error: unknown input_type '{input_type}'; must be one of {', '.join(_INPUT_TYPES)}."
    if schedule:
        try:
            CronTrigger.from_crontab(schedule)
        except Exception:
            return f"Error: invalid cron expression '{schedule}'."
    return None


async def list_automations() -> str:
    """List all automations with their id, name, type, schedule, and enabled state."""
    async with async_session() as session:
        automations = await _list(session)
    if not automations:
        return "No automations found."
    return "\n".join(
        f"- id={a.id} | {a.name} | {a.input_type} | "
        f"schedule={a.schedule or 'manual'} | enabled={a.enabled}"
        for a in automations
    )


async def create_automation(
    name: str,
    input_type: str,
    description: str | None = None,
    prompt_text: str | None = None,
    model: str | None = None,
    code_text: str | None = None,
    webhook_url: str | None = None,
    webhook_method: str | None = None,
    webhook_headers: str | None = None,
    webhook_body: str | None = None,
    schedule: str | None = None,
    enabled: bool = True,
    stateful: bool = False,
) -> str:
    """Create a new automation.

    Args:
        name: Human-readable name.
        input_type: "prompt" (LLM agent run), "code" (Python subprocess),
                    "webhook" (HTTP call), or "monitor" (watch something and
                    notify only when it changes — a prompt run that keeps one
                    shared conversation across runs and stays silent when the
                    agent reports NO_CHANGE).
        description: Optional short description.
        prompt_text: (prompt) The query/instruction the agent will run.
                     (monitor) The target to watch, e.g. "the latest release of X"
                     or "NVDA closing price; alert when it drops below 150".
        model: (prompt/monitor) Model ID, e.g. "google_genai:gemini-2.5-pro". None = use default.
        code_text: (code) Python source code to execute.
        webhook_url: (webhook) Target URL.
        webhook_method: (webhook) HTTP method. Defaults to "POST".
        webhook_headers: (webhook) JSON string of headers.
        webhook_body: (webhook) Raw request body.
        schedule: Cron expression for recurring runs, e.g. "0 9 * * *" = daily 9am.
                  None = manual trigger only.
        enabled: Whether the automation is active. Default True.
        stateful: (prompt) Share one conversation across runs so the agent
                  remembers previous runs. Monitors are always stateful.
    """
    error = _invalid_input(input_type, schedule)
    if error:
        return error
    async with async_session() as session:
        auto = await _create(
            session,
            name=name,
            description=description,
            input_type=input_type,
            prompt_text=prompt_text,
            model=model,
            code_text=code_text,
            webhook_url=webhook_url,
            webhook_method=webhook_method,
            webhook_headers=webhook_headers,
            webhook_body=webhook_body,
            schedule=schedule,
            enabled=enabled,
            stateful=stateful,
        )
    if auto.enabled and auto.schedule:
        _register_scheduler_job(auto)
    return (
        f"Created automation '{auto.name}' "
        f"(id={auto.id}, type={auto.input_type}, schedule={auto.schedule or 'manual'})."
    )


async def update_automation(automation_id: str, **fields) -> str:
    """Update fields on an existing automation.

    Pass the automation id and any subset of the same keyword arguments
    accepted by create_automation (e.g. name, schedule, enabled, prompt_text).
    """
    error = _invalid_input(fields.get("input_type"), fields.get("schedule"))
    if error:
        return error
    async with async_session() as session:
        auto = await _update(session, automation_id, **fields)
    if auto is None:
        return f"Automation '{automation_id}' not found."
    _remove_scheduler_job(automation_id)
    if auto.enabled and auto.schedule:
        _register_scheduler_job(auto)
    return f"Updated automation '{auto.name}' (id={auto.id})."


async def delete_automation(automation_id: str) -> str:
    """Delete an automation by its id. Requires approval."""
    from core.approval import request_tool_approval

    if not request_tool_approval(
        "delete_automation",
        {"automation_id": automation_id},
        f"Delete automation {automation_id}? This cannot be undone.",
    ):
        return "User denied approval — not deleting automation."

    _remove_scheduler_job(automation_id)
    async with async_session() as session:
        deleted = await _delete(session, automation_id)
    return (
        f"Deleted automation {automation_id}."
        if deleted
        else f"Automation '{automation_id}' not found."
    )


async def manage_automations(
    action: str,
    automation_id: str | None = None,
    name: str | None = None,
    input_type: str | None = None,
    description: str | None = None,
    prompt_text: str | None = None,
    model: str | None = None,
    code_text: str | None = None,
    webhook_url: str | None = None,
    webhook_method: str | None = None,
    webhook_headers: str | None = None,
    webhook_body: str | None = None,
    schedule: str | None = None,
    enabled: bool | None = None,
    stateful: bool | None = None,
) -> str:
    """List, create, update, or delete automations (scheduled or on-demand tasks).

    action: "list" | "create" | "update" | "delete".
    create requires name + input_type — "prompt" (agent run: prompt_text,
    optional model id, stateful=True shares one conversation across runs),
    "code" (code_text runs as a Python subprocess), "webhook" (webhook_url +
    optional method/headers-JSON/body), or "monitor" (always-stateful prompt
    run that watches prompt_text's target, e.g. "NVDA close; alert below 150",
    and notifies only on change). schedule is a cron expression ("0 9 * * *" =
    daily 9am); None/empty = manual trigger only. update takes automation_id
    plus only the fields to change (empty string clears a text field).
    delete takes automation_id.
    """
    action = (action or "").strip().lower()
    if action == "list":
        return await list_automations()
    if action == "create":
        if not name or not input_type:
            return "Error: create requires name and input_type."
        return await create_automation(
            name=name,
            input_type=input_type,
            description=description,
            prompt_text=prompt_text,
            model=model,
            code_text=code_text,
            webhook_url=webhook_url,
            webhook_method=webhook_method,
            webhook_headers=webhook_headers,
            webhook_body=webhook_body,
            schedule=schedule,
            enabled=True if enabled is None else enabled,
            stateful=bool(stateful),
        )
    if action == "update":
        if not automation_id:
            return "Error: update requires automation_id."
        fields = {
            k: v
            for k, v in dict(
                name=name,
                input_type=input_type,
                description=description,
                prompt_text=prompt_text,
                model=model,
                code_text=code_text,
                webhook_url=webhook_url,
                webhook_method=webhook_method,
                webhook_headers=webhook_headers,
                webhook_body=webhook_body,
                schedule=schedule,
                enabled=enabled,
                stateful=stateful,
            ).items()
            if v is not None
        }
        if not fields:
            return "Error: update requires at least one field to change."
        return await update_automation(automation_id, **fields)
    if action == "delete":
        if not automation_id:
            return "Error: delete requires automation_id."
        return await delete_automation(automation_id)
    return f"Error: unknown action '{action}'; must be one of list, create, update, delete."
