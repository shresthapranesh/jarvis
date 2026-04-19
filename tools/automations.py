"""Agent tools for managing automations via the Jarvis database."""
from __future__ import annotations

from db.engine import async_session
from db.ops import (
    create_automation as _create,
    delete_automation as _delete,
    list_automations as _list,
    update_automation as _update,
)


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
) -> str:
    """Create a new automation.

    Args:
        name: Human-readable name.
        input_type: "prompt" (LLM agent run), "code" (Python subprocess), or "webhook" (HTTP call).
        description: Optional short description.
        prompt_text: (prompt) The query/instruction the agent will run.
        model: (prompt) Model ID, e.g. "google_genai:gemini-2.5-pro". None = use default.
        code_text: (code) Python source code to execute.
        webhook_url: (webhook) Target URL.
        webhook_method: (webhook) HTTP method. Defaults to "POST".
        webhook_headers: (webhook) JSON string of headers.
        webhook_body: (webhook) Raw request body.
        schedule: Cron expression for recurring runs, e.g. "0 9 * * *" = daily 9am.
                  None = manual trigger only.
        enabled: Whether the automation is active. Default True.
    """
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
        )
    return (
        f"Created automation '{auto.name}' "
        f"(id={auto.id}, type={auto.input_type}, schedule={auto.schedule or 'manual'})."
    )


async def update_automation(automation_id: str, **fields) -> str:
    """Update fields on an existing automation.

    Pass the automation id and any subset of the same keyword arguments
    accepted by create_automation (e.g. name, schedule, enabled, prompt_text).
    """
    async with async_session() as session:
        auto = await _update(session, automation_id, **fields)
    if auto is None:
        return f"Automation '{automation_id}' not found."
    return f"Updated automation '{auto.name}' (id={auto.id})."


async def delete_automation(automation_id: str) -> str:
    """Delete an automation by its id."""
    async with async_session() as session:
        deleted = await _delete(session, automation_id)
    return (
        f"Deleted automation {automation_id}."
        if deleted
        else f"Automation '{automation_id}' not found."
    )
